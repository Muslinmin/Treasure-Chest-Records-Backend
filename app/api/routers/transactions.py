from fastapi import APIRouter
from datetime import date

from app.api.auth.api_key import verify_api_key

from fastapi import Depends

from app.db.session import get_db

from sqlalchemy.orm import Session

router = APIRouter(dependencies=[Depends(verify_api_key)])


@router.get("/transactions", tags=["transactions"])
async def get_transactions(db : Session = Depends(get_db), date_from : date | None = None, date_to: date | None = None, category: str | None = None, retrieve_limit: int = 50, offset: int = 0):
    
    pass