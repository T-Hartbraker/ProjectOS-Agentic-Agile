"""Health routes. Component readiness is visible; failures are not painted green."""

from __future__ import annotations

from fastapi import APIRouter, Request

from projectos.http.schemas import HealthResponse
from projectos.operator import operator_health
from projectos.services.context import ServiceContext

router = APIRouter(tags=["health"])


def _health(request: Request) -> HealthResponse:
    ctx: ServiceContext = request.app.state.service_context
    return HealthResponse.model_validate(operator_health(ctx))


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    return _health(request)


@router.get("/v1/health", response_model=HealthResponse)
def health_v1(request: Request) -> HealthResponse:
    return _health(request)
