from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy import UniqueConstraint, CheckConstraint, ForeignKey

from datetime import date, datetime
from sqlalchemy import Date, DateTime

"""
transactions
  id               INTEGER  PRIMARY KEY AUTOINCREMENT
  transaction_date DATE     NOT NULL
  amount_cents     INTEGER  NOT NULL        ← $12.50 stored as 1250
  description      TEXT
  transaction_code TEXT                     ← nullable, bank's internal code
  ref1             TEXT                     ← vendor name, nullable
  is_settled       BOOLEAN  NOT NULL        ← True/False only
  category         TEXT                     ← nullable, filled later
  is_category_manual BOOLEAN DEFAULT FALSE
  fingerprint      TEXT                     ← sha256 of the row's stable identity
                                              fields; indexed, NOT unique

"""


class Base(DeclarativeBase):
    pass

class Transaction(Base):
    __tablename__= "transaction_records"

    id: Mapped[int] = mapped_column(primary_key=True)

    transaction_date: Mapped[date] = mapped_column(Date)

    amount_cents: Mapped[int] = mapped_column()

    #for LLM to infer#
    description: Mapped[str | None] = mapped_column(String())

    transaction_code: Mapped[str | None] = mapped_column(String(10))

    vendor_name: Mapped[str | None] = mapped_column(String())

    category: Mapped[str | None] = mapped_column(String(100))
    ##################
    is_settled: Mapped[bool] = mapped_column(default=False)

    is_category_manual: Mapped[bool] = mapped_column(default=False)

    # sha256 over transaction_date, amount_cents, description (masked) and
    # transaction_code — the fields that identify a transaction across
    # re-exports. vendor_name is excluded: Ref1 is a stripped substring of the
    # description, so it adds nothing the hash does not already have.
    # Deliberately NOT unique: two identical purchases on the same day are
    # legitimate and share a fingerprint. Dedupe compares counts, not existence.
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)


"""
summary
  id           INTEGER  PRIMARY KEY AUTOINCREMENT
  period       TEXT     NOT NULL   ← "2025-04" year-month string
  category     TEXT     NOT NULL
  total_cents  INTEGER  NOT NULL
  tx_count     INTEGER  NOT NULL
  updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
  UNIQUE(period, category)         ← no duplicate period+category rows

"""


class Summary(Base):
    __tablename__= "Summary"
    __table_args__ = (UniqueConstraint("period", "category"),)


    id: Mapped[int] = mapped_column(primary_key=True)

    period: Mapped[str] = mapped_column(String(7))

    category: Mapped[str] = mapped_column(String(100))

    total_cents: Mapped[int] = mapped_column()

    tx_count: Mapped[int] = mapped_column()

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


"""
categories
  id          INTEGER  PRIMARY KEY AUTOINCREMENT
  name        TEXT     NOT NULL UNIQUE   ← natural key; rules.py/merchant_categories target it
  is_system   BOOLEAN  default False     ← seeded, undeletable (Unknown, Transfer In,
                                            Transfer Out, Interest, Income — see §10.5)
  is_active   BOOLEAN  default True      ← soft delete
  created_at  DATETIME default now
"""


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(String(100), unique=True)

    is_system: Mapped[bool] = mapped_column(default=False)

    is_active: Mapped[bool] = mapped_column(default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


"""
merchant_categories
  id            INTEGER  PRIMARY KEY AUTOINCREMENT
  merchant_key  TEXT     NOT NULL UNIQUE   ← normalised + clustered, see §10.2/§10.3
  category      TEXT     NOT NULL          → categories.name
                                              FOREIGN KEY ON DELETE RESTRICT ON UPDATE CASCADE
  source        TEXT     NOT NULL          ← 'rule' | 'llm' | 'manual'
  hit_count     INTEGER  default 0         ← confidence signal; singletons are
                                              where mistakes hide
  created_at / updated_at  DATETIME

`source` is what makes a scoped re-derivation safe (§10.5): only `llm` rows are
ever cleared, never `manual`. The FK targets `categories.name` rather than an
integer id — see §10.7 for why (avoids an ALTER on transaction_records.category,
the one column this feature is designed not to touch).
"""


class MerchantCategory(Base):
    __tablename__ = "merchant_categories"
    __table_args__ = (CheckConstraint("source IN ('rule', 'llm', 'manual')"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    merchant_key: Mapped[str] = mapped_column(String(), unique=True)

    category: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("categories.name", ondelete="RESTRICT", onupdate="CASCADE"),
    )

    source: Mapped[str] = mapped_column(String(10))

    hit_count: Mapped[int] = mapped_column(default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )
