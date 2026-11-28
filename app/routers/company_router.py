from fastapi import APIRouter

from ..services.stock_service import stock_service

router = APIRouter(prefix="/api")


@router.get("/stock/company_info")
def get_company_info(code: str):
    return stock_service.get_company_info(code)
