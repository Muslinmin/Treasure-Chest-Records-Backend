from pathlib import Path
import csv


"""
transactions
  transaction_date DATE     NOT NULL
  amount_cents     INTEGER  NOT NULL        ← $12.50 stored as 1250
  description      TEXT
  transaction_code TEXT                     ← nullable, bank's internal code
  vendor_name             TEXT                     ← vendor name, nullable
  is_settled       BOOLEAN  NOT NULL        ← True/False only
  category         TEXT                     ← nullable, filled later
  is_category_manual BOOLEAN DEFAULT FALSE
  source_file      TEXT                     ← which CSV this came from

"""


from datetime import datetime

import logging
import os

logger = logging.getLogger(__name__)

def parse_csv(filepath: Path) -> list[dict]:
    csv_file = Path(filepath)
    records = list()
    with csv_file.open(mode="r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("Transaction Date"):
                headers = next(csv.reader([line]))
                break
        reader = csv.DictReader(f, fieldnames=headers)
        for row in reader:
            row_dict = {} #initialize a new row dictionary for clean tracking below:
            if not row["Transaction Date"]:
                continue
            else:
                dt_object = datetime.strptime(row["Transaction Date"], "%d-%b-%y")
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

                row_dict["description"] = row["Description"]

                row_dict["transaction_code"] = row["Transaction Code"]

                row_dict["vendor_name"] = row["Transaction Ref1"]

                settled_status = row["Status"].lower()

                if settled_status == "settled":
                    row_dict["is_settled"] = True
                else:
                    row_dict["is_settled"] = False

                row_dict["category"] = None

                row_dict["is_category_manual"] = False
                

                file_name = os.path.basename(filepath)
                row_dict["source_file"] = file_name
            
                records.append(row_dict)
    if not records:
        raise Exception(f"Records are empty ! {records}")
    return records