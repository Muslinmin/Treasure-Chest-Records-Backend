from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.api.auth.api_key import verify_api_key

from pydantic import BaseModel, ConfigDict

from app.db import queries
from app.summary.aggregator import recompute_summary
from app.categorise.hierarchy import carve_category
from app.categorise.service import categorise as run_categorisation
from app.llm.categoriser import Categoriser as LLMCategoriser


router = APIRouter(dependencies=[Depends(verify_api_key)])


class CategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str
    is_system: bool
    is_active: bool
    created_at: datetime
    parent_name: str | None = None


class CreateCategoryRequest(BaseModel):
    name: str
    carved_from: list[str] = []


class CarveCategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    category: CategoryResponse
    catch_all_created: str | None = None
    rederivation: dict | None = None
    recomputed_periods: list[str] = []


@router.post("/categorise", tags=["categories"])
def run_categorise(db: Session = Depends(get_db)):
    """Standalone run over category IS NULL AND is_category_manual = 0 (§10.9).

    The resumability and recovery path — also how the one-time backfill over
    already-ingested, still-uncategorised rows is run.
    """
    return run_categorisation(db, LLMCategoriser())


@router.get("/categories", tags=["categories"], response_model=list[CategoryResponse])
def get_categories(db: Session = Depends(get_db), include_inactive: bool = False):
    return queries.list_categories_with_parent_names(db, include_inactive)


@router.post("/categories", tags=["categories"], response_model=CarveCategoryResponse)
def add_category(request: CreateCategoryRequest, db: Session = Depends(get_db)):
    try:
        return carve_category(db, LLMCategoriser(), request.name, request.carved_from)
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/categories/{name}", tags=["categories"])
def delete_category(name: str, db: Session = Depends(get_db), reassign_to: str | None = None):
    try:
        if reassign_to is None:
            queries.soft_delete_category(db, name)
            db.commit()
            return {"name": name, "status": "deactivated"}

        affected_periods = queries.hard_delete_category(db, name, reassign_to)
        for period in affected_periods:
            recompute_summary(db, period)
        db.commit()
        return {
            "name": name,
            "status": "deleted",
            "reassigned_to": reassign_to,
            "recomputed_periods": sorted(affected_periods),
        }
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
