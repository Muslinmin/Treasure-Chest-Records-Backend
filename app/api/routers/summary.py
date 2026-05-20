import os
from pathlib import Path
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.api.auth.api_key import verify_api_key

router = APIRouter(dependencies=[Depends(verify_api_key)])

@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    pass
