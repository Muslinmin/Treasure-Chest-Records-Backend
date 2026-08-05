"""
transactions
  transaction_date DATE     NOT NULL
  amount_cents     INTEGER  NOT NULL        ← $12.50 stored as 1250
  description      TEXT                     ← card number masked before storage
  transaction_code TEXT                     ← nullable, bank's internal code
  vendor_name      TEXT                     ← vendor name, nullable, display only
  is_settled       BOOLEAN  NOT NULL        ← True/False only
  category         TEXT                     ← nullable, filled later
  is_category_manual BOOLEAN DEFAULT FALSE
  fingerprint      TEXT                     ← sha256 row identity, see identity.py

`Transaction Ref2` and `Transaction Ref3` are read by the DictReader and
deliberately discarded: they are stripped substrings of `Description`, so they
carry nothing the description does not already contain. Ref2 is the masked card
number; Ref3 is the payment-network reference, which for card rows makes
`Description` unique per transaction.

The filename is not recorded. Identity is a property of the transaction, not of
the export it arrived in.
"""

import csv
import logging
from datetime import datetime
from pathlib import Path

from app.ingest.identity import compute_fingerprint, mask_card

logger = logging.getLogger(__name__)

def parse_csv(filepath: Path) -> list[dict]:
    csv_file = Path(filepath)
    records = list()
    with csv_file.open(mode="r", encoding="utf-8-sig") as f:
        for line in f:
            if "Transaction Date" in line:
                headers = next(csv.reader([line]))
                break
        reader = csv.DictReader(f, fieldnames=headers)
        for row in reader:
            row_dict = {} #initialize a new row dictionary for clean tracking below:
            if not row["Transaction Date"]:
                continue
            else:
                dt_object = datetime.strptime(row["Transaction Date"], "%d %b %Y")
                row_dict["transaction_date"] = dt_object.date()

                amount_cents = int()
                if row["Debit Amount"]:
                    amount_cents = int(float(row["Debit Amount"]) * 100 * -1)
                    del row["Credit Amount"]
                elif row["Credit Amount"]:
                    amount_cents = int(float(row["Credit Amount"]) * 100)
                    del row["Debit Amount"]
                else:
                    logger.warning(f"Row skipped — no debit or credit amount: {row}")
                    # @todo how to handle rows with no amount in both credit and debit?
                    continue
                row_dict["amount_cents"] = amount_cents

                # Masked before storage *and* before hashing, so the stored
                # value and the hashed value can never diverge.
                row_dict["description"] = mask_card(row["Description"])

                row_dict["transaction_code"] = row["Transaction Code"]

                row_dict["vendor_name"] = row["Transaction Ref1"]

                settled_status = row["Status"].lower()

                if settled_status == "settled":
                    row_dict["is_settled"] = True
                else:
                    row_dict["is_settled"] = False

                row_dict["category"] = None

                row_dict["is_category_manual"] = False

                row_dict["fingerprint"] = compute_fingerprint(
                    row_dict["transaction_date"],
                    row_dict["amount_cents"],
                    row_dict["description"],
                    row_dict["transaction_code"],
                )

                records.append(row_dict)
    if not records:
        raise Exception(f"Records are empty ! {records}")
    return records