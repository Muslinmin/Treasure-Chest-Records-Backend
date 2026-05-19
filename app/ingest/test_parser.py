from pathlib import Path
import pytest
from csv_parser import parse_csv 
import datetime

def test_parse_csv_retrieves_output():
    file_path = r"C:\Users\lordm\OneDrive\Desktop\Projects\Treasure-Chest-Records-Backend\data\inbox\test.csv"
    result = parse_csv(file_path)
    assert len(result) == 3
    
    assert result[0]["transaction_date"] == datetime.date(2026, 5, 8)
    assert result[0]["description"] == "SUBWAY @ SMU SINGAPORE SGP 06MAY xxxx-xxxx-xxxx-xxxx 000002391139531"
    
    assert result[1]["transaction_date"] == datetime.date(2026, 5, 8)
    assert result[1]["description"] == "NTUC FairPrice App Pay SI SGP 05MAY 5264-7110-2059-4505 000002394032280"

    assert result[2]["transaction_date"] == datetime.date(2026, 6, 12)
    assert result[2]["description"] == "Payment from Boss"

def test_amount_cents_retrieved():
    file_path = r"C:\Users\lordm\OneDrive\Desktop\Projects\Treasure-Chest-Records-Backend\data\inbox\test.csv"
    result = parse_csv(file_path)
    assert len(result) == 3
    assert result[0]["amount_cents"] == -1320
    assert "Debit Amount" not in result[0]
    assert "Credit Amount" not in result[0]
    assert result[1]["amount_cents"] == -340
    assert "Debit Amount" not in result[1]
    assert "Credit Amount" not in result[1]
    assert result[2]["amount_cents"] == 15000
    assert "Debit Amount" not in result[2]
    assert "Credit Amount" not in result[2]

def test_parse_csv_raises_exception_if_empty():
    file_path = r"C:\Users\lordm\OneDrive\Desktop\Projects\Treasure-Chest-Records-Backend\data\inbox\test_exec.csv"
    with pytest.raises(Exception) as exec_info:
            parse_csv(file_path)
            assert("Records are empty" in exec_info.value)



