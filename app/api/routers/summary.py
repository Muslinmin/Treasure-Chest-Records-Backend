from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.api.auth.api_key import verify_api_key

from pydantic import BaseModel, ConfigDict, computed_field

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
    
    @computed_field
    @property
    def amount(self) -> float:
        return self.total_cents/100


@router.get("/summary", tags=["summary"], response_model=list[SummaryResponse])
def get_summary(db: Session = Depends(get_db), period: str | None = None):
    if period is None:
        period = date.today().strftime("%Y-%m")
    result = queries.get_summary_by_period(db, period)
    return result


@router.get("/summary/monthly", tags=["summary"], response_model=list[SummaryResponse])
def get_monthly_summary(db: Session = Depends(get_db)):
    today = date.today()
    end_period = today.strftime("%Y-%m")
    start_period = date(today.year - 1, today.month, 1).strftime("%Y-%m")
    result = queries.get_summary_monthly(db, start_period, end_period)
    return result
