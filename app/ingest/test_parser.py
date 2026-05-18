from pathlib import Path
import pytest
from parser import parse_csv 
import datetime

def test_parse_csv_retrieves_output():
    file_path = r"C:\Users\lordm\OneDrive\Desktop\Projects\Treasure-Chest-Records-Backend\data\inbox\test.csv"
    result = parse_csv(file_path)
    assert len(result) == 2
    
    assert result[0]["Transaction Date"] == datetime.date(2026, 5, 8)
    assert result[0]["Debit Amount"] == "13.2"
    assert result[0]["Description"] == "SUBWAY @ SMU SINGAPORE SGP 06MAY xxxx-xxxx-xxxx-xxxx 000002391139531"
    
    assert result[1]["Transaction Date"] == datetime.date(2026, 5, 8)
    assert result[1]["Debit Amount"] == "3.4"
    assert result[1]["Description"] == "NTUC FairPrice App Pay SI SGP 05MAY 5264-7110-2059-4505 000002394032280"



def test_parse_csv_raises_exception_if_empty():
    file_path = r"C:\Users\lordm\OneDrive\Desktop\Projects\Treasure-Chest-Records-Backend\data\inbox\test_exec.csv"
    with pytest.raises(Exception) as exec_info:
            parse_csv(file_path)
            assert("Records are empty" in exec_info.value)
