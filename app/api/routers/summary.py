from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.api.auth.api_key import verify_api_key

from pydantic import BaseModel, ConfigDict

from app.db import queries



from datetime import datetime, date


router = APIRouter(dependencies=[Depends(verify_api_key)])


class SummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    period : str
    category : str
    total_cents: int
    tx_count : int
    updated_at : datetime
    

@router.get("/summary", tags=["summary"], response_model=list[SummaryResponse])
def get_summary(db: Session = Depends(get_db), period: str | None = None, rollup: bool = False):
    if period is None:
        period = date.today().strftime("%Y-%m")
    result = queries.get_summary_by_period(db, period, rollup=rollup)
    return result


@router.get("/summary/monthly", tags=["summary"], response_model=list[SummaryResponse])
def get_monthly_summary(db: Session = Depends(get_db), rollup: bool = False):
    today = date.today()
    end_period = today.strftime("%Y-%m")
    start_period = date(today.year - 1, today.month, 1).strftime("%Y-%m")
    result = queries.get_summary_monthly(db, start_period, end_period, rollup=rollup)
    return result
