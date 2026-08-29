"""The only normal cross-project view. Does not merge project stores."""

from __future__ import annotations

from fastapi import APIRouter, Request

from projectos.http.deps import get_service_context
from projectos.http.schemas import PortfolioResponse
from projectos.portfolio import build_portfolio

router = APIRouter(prefix="/v1", tags=["portfolio"])


@router.get("/portfolio", response_model=PortfolioResponse)
def portfolio(request: Request) -> PortfolioResponse:
    ctx = get_service_context(request)
    return PortfolioResponse.model_validate(build_portfolio(ctx))
