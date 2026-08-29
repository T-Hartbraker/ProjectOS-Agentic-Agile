"""Polled project projection. Stable read model; no orchestration tables."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from projectos.http.deps import get_projection_service
from projectos.http.schemas import ProjectionResponse
from projectos.services.projection import ProjectionService

router = APIRouter(prefix="/v1/projects", tags=["projection"])


@router.get("/{project_human_id}/projection", response_model=ProjectionResponse)
def project_projection(
    project_human_id: str,
    request: Request,
    svc: ProjectionService = Depends(get_projection_service),
) -> ProjectionResponse | JSONResponse:
    snapshot = svc.snapshot(project_human_id)
    etag = snapshot.etag()
    incoming = (request.headers.get("if-none-match") or "").strip()
    if incoming and incoming == etag:
        return JSONResponse(status_code=304, content=None, headers={"ETag": etag})
    payload = ProjectionResponse.model_validate(snapshot.as_public_dict())
    return JSONResponse(
        content=payload.model_dump(),
        headers={"ETag": etag, "Cache-Control": "no-store"},
    )
