from fastapi import APIRouter
from datetime import date

from app.api.auth.api_key import verify_api_key

from fastapi import Depends

from app.db.session import get_db

from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict
from app.db import queries


router = APIRouter(dependencies=[Depends(verify_api_key)])


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    transaction_date: date
    amount_cents: int
    description: str | None
    transaction_code: str | None
    vendor_name : str | None
    category: str | None
    is_settled: bool
    is_category_manual : bool


@router.get("/transactions", tags=["transactions"], response_model=list[TransactionResponse])
async def get_transactions(db : Session = Depends(get_db), date_from : date | None = None, date_to: date | None = None, category: str | None = None, retrieve_limit: int = 50, offset: int = 0):
    return queries.get_transactions(db, retrieve_limit, offset, date_from, date_to, category)