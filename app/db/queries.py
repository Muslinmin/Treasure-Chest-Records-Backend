
from app.db.models import Transaction, Summary, Category, MerchantCategory, IngestJob
from sqlalchemy.orm import Session
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from collections import Counter
from datetime import date, datetime, timedelta
import json
import logging
import uuid


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


def insert_records(db: Session, records: list[dict], collect_ids: list[int] | None = None) -> dict:
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

    ``collect_ids``, if given, is extended with the ids of the rows actually
    inserted (requires a ``flush`` to populate autoincrement ids — skipped
    entirely when no caller asks for them). This is what lets a caller scope
    later work — e.g. categorisation — to exactly the rows one upload added,
    rather than every uncategorised row in the table.
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
    new_rows = [] if collect_ids is not None else None

    for record in records:
        fingerprint = record["fingerprint"]
        seen[fingerprint] += 1
        if seen[fingerprint] <= existing.get(fingerprint, 0):
            skipped += 1
            continue
        row = Transaction(**record)
        db.add(row)
        inserted += 1
        if new_rows is not None:
            new_rows.append(row)

    if new_rows:
        db.flush()
        collect_ids.extend(row.id for row in new_rows)

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


def _rollup_summary_rows(db: Session, records: list[Summary]) -> list[dict]:
    """Group leaf ``Summary`` rows by ``COALESCE(parent_name, category)`` and sum.

    Never stored — computed at read time over whatever rows the period query
    already returned (a handful per period regardless of transaction count),
    so the delete-and-reinsert write path in aggregator.py stays untouched.
    A category name with no matching row in ``categories`` (e.g. the
    aggregator's synthetic "Uncategorised") rolls up to itself.
    """
    if not records:
        return []

    id_to_name = dict(db.execute(select(Category.id, Category.name)).all())
    name_to_parent_id = dict(db.execute(select(Category.name, Category.parent_id)).all())

    grouped: dict[tuple[str, str], dict] = {}
    for row in records:
        parent_id = name_to_parent_id.get(row.category)
        group_name = id_to_name.get(parent_id, row.category) if parent_id else row.category
        bucket_key = (row.period, group_name)
        bucket = grouped.setdefault(
            bucket_key,
            {
                "period": row.period,
                "category": group_name,
                "total_cents": 0,
                "tx_count": 0,
                "updated_at": row.updated_at,
            },
        )
        bucket["total_cents"] += row.total_cents
        bucket["tx_count"] += row.tx_count
        if row.updated_at > bucket["updated_at"]:
            bucket["updated_at"] = row.updated_at

    return list(grouped.values())


def get_summary_by_period(db: Session, period: str, rollup: bool = False) -> list[Summary] | list[dict]:
    stmt = select(Summary)
    stmt = stmt.where(Summary.period == period)
    records = db.scalars(stmt).all()
    logger.info(f"Summary for {period} retrieved!")
    return _rollup_summary_rows(db, records) if rollup else records


def get_summary_monthly(
    db: Session, start_period: str, end_period: str, rollup: bool = False
) -> list[Summary] | list[dict]:
    stmt = select(Summary)

    stmt= stmt.where(Summary.period >= start_period, Summary.period <= end_period)


    records = db.scalars(stmt).all()
    logger.info(f"Summary for period range {start_period} and {end_period} retrieved!")

    return _rollup_summary_rows(db, records) if rollup else records


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


def list_categories_with_parent_names(db: Session, include_inactive: bool = False) -> list[dict]:
    """``list_categories`` plus each row's parent name, for the API surface.

    Looked up against every category (not just the filtered set), so a child
    row still resolves its parent's name even if the parent itself is
    filtered out of an ``include_inactive=False`` listing.
    """
    categories = list_categories(db, include_inactive)
    id_to_name = dict(db.execute(select(Category.id, Category.name)).all())
    return [
        {
            "name": c.name,
            "is_system": c.is_system,
            "is_active": c.is_active,
            "created_at": c.created_at,
            "parent_name": id_to_name.get(c.parent_id),
        }
        for c in categories
    ]


def list_leaf_categories(db: Session, include_inactive: bool = False) -> list[Category]:
    """Categories eligible to hold transactions directly — excludes stems.

    Once something has been carved from a category it is permanently a stem:
    a pure grouping label with zero transactions of its own (§Planned:
    Category Hierarchy). The rules/LLM taxonomy must never offer a stem as an
    answer — a fresh transaction landing directly on it would double-count
    against its own children once ``?rollup=true`` sums by parent.
    """
    stems = select(Category.parent_id).where(Category.parent_id.is_not(None))
    stmt = select(Category).where(Category.id.notin_(stems))
    if not include_inactive:
        stmt = stmt.where(Category.is_active.is_(True))
    stmt = stmt.order_by(Category.name)
    return db.scalars(stmt).all()


def get_category(db: Session, name: str) -> Category | None:
    return db.scalars(select(Category).where(Category.name == name)).first()


def get_children(db: Session, parent_name: str) -> list[Category]:
    parent = get_category(db, parent_name)
    if parent is None:
        return []
    return db.scalars(select(Category).where(Category.parent_id == parent.id)).all()


def has_children(db: Session, category: Category) -> bool:
    return db.scalar(select(Category.id).where(Category.parent_id == category.id).limit(1)) is not None


def catch_all_name(stem_name: str) -> str:
    return f"{stem_name} (Other)"


def create_category(db: Session, name: str, carved_from: list[str]) -> dict:
    """Add a category to the taxonomy (§10.5), carving it out of a parent if asked.

    ``carved_from`` states scope: ``[]`` is a pure addition — only merchants
    seen for the first time after this point can receive it, and the new row
    is created top-level (no parent).

    A non-empty ``carved_from`` must name exactly one category (multi-parent
    "DAG" categories are out of scope — see Planned: Category Hierarchy), and
    that category must currently be top-level (``parent_id IS NULL``):
    carving from an already-carved leaf is rejected, which is what caps the
    tree at exactly two levels by construction.

    The first carve under a given parent promotes it to a stem and
    auto-generates a ``"<Stem> (Other)"`` catch-all sibling leaf, migrating
    every transaction and merchant-cache row currently filed directly under
    the parent's own name onto that catch-all — a stem must hold zero
    transactions directly from the moment it becomes a stem. This function
    only does that migration (bulk data movement); re-deriving which
    catch-all merchants actually belong under the *new* leaf is
    hierarchy.carve_category's job, since that needs the LLM.

    Returns a dict: ``category`` (the new row), ``catch_all`` (the name of
    the stem's catch-all leaf, or ``None`` if this was a pure top-level
    addition), ``catch_all_created`` (whether this call is what created it),
    and ``affected_periods`` (periods touched by the parent -> catch-all
    migration, empty unless this was the first carve).
    """
    if get_category(db, name) is not None:
        raise ValueError(f"category '{name}' already exists")

    if len(carved_from) > 1:
        raise ValueError("carved_from supports at most one parent category (no multi-parent categories)")

    if not carved_from:
        category = Category(name=name, is_system=False, is_active=True, parent_id=None)
        db.add(category)
        db.flush()
        return {
            "category": category,
            "catch_all": None,
            "catch_all_created": False,
            "affected_periods": set(),
        }

    parent_name = carved_from[0]
    parent = get_category(db, parent_name)
    if parent is None or not parent.is_active:
        raise ValueError(f"carved_from parent '{parent_name}' is not an active category")
    if parent.parent_id is not None:
        raise ValueError(f"'{parent_name}' already has a parent and cannot itself be carved from")

    first_carve = not has_children(db, parent)

    category = Category(name=name, is_system=False, is_active=True, parent_id=parent.id)
    db.add(category)
    db.flush()

    catch_all = catch_all_name(parent.name)
    affected_periods: set[str] = set()

    if first_carve:
        if get_category(db, catch_all) is not None:
            raise ValueError(f"catch-all category '{catch_all}' already exists unexpectedly")

        db.add(Category(name=catch_all, is_system=False, is_active=True, parent_id=parent.id))
        db.flush()

        affected_dates = db.scalars(
            select(Transaction.transaction_date).where(Transaction.category == parent.name)
        ).all()
        affected_periods = {d.strftime("%Y-%m") for d in affected_dates}

        db.execute(
            Transaction.__table__.update()
            .where(Transaction.category == parent.name)
            .values(category=catch_all)
        )
        db.execute(
            MerchantCategory.__table__.update()
            .where(MerchantCategory.category == parent.name)
            .values(category=catch_all)
        )

    return {
        "category": category,
        "catch_all": catch_all,
        "catch_all_created": first_carve,
        "affected_periods": affected_periods,
    }


def soft_delete_category(db: Session, name: str) -> Category:
    """Deactivate a category. Rows already carrying it keep their label."""
    category = get_category(db, name)
    if category is None:
        raise ValueError(f"category '{name}' does not exist")
    if category.is_system:
        raise ValueError(f"'{name}' is a system category and cannot be deleted")
    if has_children(db, category):
        raise ValueError(f"'{name}' has subcategories carved from it and cannot be deleted directly")

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
    if has_children(db, category):
        raise ValueError(f"'{name}' has subcategories carved from it and cannot be deleted directly")

    target = get_category(db, reassign_to)
    if target is None or not target.is_active:
        raise ValueError(f"reassign_to target '{reassign_to}' is not an active category")
    if has_children(db, target):
        raise ValueError(f"reassign_to target '{reassign_to}' is a parent category and cannot hold transactions directly")

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


def get_uncategorised(db: Session, transaction_ids: list[int] | None = None) -> list[Transaction]:
    """Rows §10.1's pipeline still needs to resolve.

    ``is_category_manual`` rows are excluded even if ``category`` happens to
    be null — a manual edit that clears a category is still a manual
    decision, not an invitation to auto-categorise.

    ``transaction_ids``, if given, further restricts the result to just
    those rows — how an ingest job scopes categorisation to the upload that
    triggered it, instead of every uncategorised row in the table.
    """
    stmt = select(Transaction).where(
        Transaction.category.is_(None), Transaction.is_category_manual.is_(False)
    )
    if transaction_ids is not None:
        stmt = stmt.where(Transaction.id.in_(transaction_ids))
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


STALE_JOB_AFTER = timedelta(minutes=10)


def create_ingest_job(db: Session) -> IngestJob:
    """Register a new ingest job, ``pending`` until its background run starts."""
    job = IngestJob(id=str(uuid.uuid4()), status="pending")
    db.add(job)
    db.flush()
    return job


def update_ingest_job_status(
    db: Session, job_id: str, status: str, result: dict | None = None, error: str | None = None
) -> None:
    job = db.get(IngestJob, job_id)
    if job is None:
        return
    job.status = status
    if result is not None:
        job.result = json.dumps(result)
    if error is not None:
        job.error = error


def get_ingest_job(db: Session, job_id: str, stale_after: timedelta = STALE_JOB_AFTER) -> IngestJob | None:
    """Look up a job, reconciling it first if it's gone stale.

    A job stuck ``running`` with no update in over ``stale_after`` means
    whatever was executing it died without a clean failure (a hung provider
    call, a killed worker) — nothing will ever move it to a terminal state on
    its own. Caught here, lazily, on the next poll: no separate sweeper
    process needed, and a client polling this endpoint always converges to a
    terminal status instead of waiting forever.
    """
    job = db.get(IngestJob, job_id)
    if job is None:
        return None
    if job.status == "running" and datetime.now() - job.updated_at > stale_after:
        job.status = "failed"
        job.error = "stale: no progress detected"
    return job


def reconcile_orphaned_jobs(db: Session) -> int:
    """Close out jobs left mid-flight by an unclean shutdown.

    Jobs run as in-process background tasks, so nothing survives a process
    restart to finish one — anything still ``pending``/``running`` at boot
    time is provably orphaned, not just slow. Call once at startup. Returns
    the number of jobs reconciled.
    """
    stmt = select(IngestJob).where(IngestJob.status.in_(["pending", "running"]))
    orphaned = db.scalars(stmt).all()
    for job in orphaned:
        job.status = "failed"
        job.error = "interrupted by server restart"
    return len(orphaned)