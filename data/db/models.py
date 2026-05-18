from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy import UniqueConstraint

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
  source_file      TEXT                     ← which CSV this came from

"""


class Base(DeclarativeBase):
    pass

class Transaction(Base):
    __tablename__= "transaction_records"

    id: Mapped[int] = mapped_column(primary_key=True)

    record_date: Mapped[date] = mapped_column(Date)

    amount_cents: Mapped[int] = mapped_column()

    #for LLM to infer
    description: Mapped[str | None] = mapped_column(String())

    transaction_code: Mapped[str | None] = mapped_column(String(10))

    vendor_name: Mapped[str | None] = mapped_column(String())

    is_settled: Mapped[bool] = mapped_column(default=False)

    category: Mapped[str | None] = mapped_column(String(100))

    is_category_manual: Mapped[bool] = mapped_column(default=False)

    source_file: Mapped[str | None] = mapped_column(String())


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

 
