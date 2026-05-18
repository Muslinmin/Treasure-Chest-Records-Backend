from pathlib import Path
import csv
# from dotenv import load_dotenv
# import os

# load_dotenv()

# CSV_INBOX = os.getenv("CSV_INBOX")

def parse_csv(filepath: Path) -> list[dict]:
    csv_file = Path(filepath)
    records = list()
    with csv_file.open(mode="r", encoding="utf-8") as f:
        for i in range(6):
            f.readline()

        reader = csv.DictReader(f)
        for row in reader:
            if "Transaction Date" in row:
                records.append(row)
    if not records:
        raise Exception(f"Records are empty ! {records}")
    return records