"""Delivery and release HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from projectos.delivery.service import DeliveryService
from projectos.github.secret_setup import apply_github_token, probe_github_connection, read_github_settings, remove_github_token
from projectos.http.deps import get_service_context
from projectos.services.context import ServiceContext

router = APIRouter(prefix="/v1", tags=["delivery"])


def _delivery(ctx: ServiceContext) -> DeliveryService:
    return DeliveryService(ctx)


@router.get("/projects/{project_human_id}/delivery")
def get_delivery_contract(
    project_human_id: str,
    ctx: ServiceContext = Depends(get_service_context),
) -> dict:
    return _delivery(ctx).show_contract(project_human_id)


@router.post("/projects/{project_human_id}/delivery/validate")
def post_delivery_validate(
    project_human_id: str,
    ctx: ServiceContext = Depends(get_service_context),
) -> dict:
    return _delivery(ctx).validate_contract(project_human_id)


@router.get("/projects/{project_human_id}/delivery/releases")
def list_delivery_releases(
    project_human_id: str,
    ctx: ServiceContext = Depends(get_service_context),
) -> dict:
    return _delivery(ctx).list_releases(project_human_id)


@router.get("/projects/{project_human_id}/delivery/blockers")
def get_delivery_blockers(
    project_human_id: str,
    ctx: ServiceContext = Depends(get_service_context),
) -> dict:
    return _delivery(ctx).release_blockers(project_human_id)


@router.post("/projects/{project_human_id}/delivery/releases/prepare")
def post_prepare_release(
    project_human_id: str,
    body: dict,
    ctx: ServiceContext = Depends(get_service_context),
) -> dict:
    return _delivery(ctx).prepare_release(
        project_human_id,
        release_human_id=str(body.get("release_human_id") or ""),
        version=str(body.get("version") or ""),
        candidate_git_sha=body.get("candidate_git_sha"),
        proposal_id=body.get("proposal_id"),
        sponsor_user_id=body.get("sponsor_user_id"),
    )


@router.get("/delivery/releases/{release_record_id}")
def get_delivery_release(
    release_record_id: str,
    ctx: ServiceContext = Depends(get_service_context),
) -> dict:
    return _delivery(ctx).get_release(release_record_id)


@router.post("/delivery/releases/{release_record_id}/package")
def post_package_release(
    release_record_id: str,
    body: dict | None = None,
    ctx: ServiceContext = Depends(get_service_context),
) -> dict:
    payload = body or {}
    return _delivery(ctx).package_release(
        release_record_id,
        executor=str(payload.get("executor") or "LOCAL"),
    )


@router.post("/delivery/releases/{release_record_id}/verify")
def post_verify_release(
    release_record_id: str,
    ctx: ServiceContext = Depends(get_service_context),
) -> dict:
    return _delivery(ctx).verify_release(release_record_id)


@router.post("/delivery/releases/{release_record_id}/publish")
def post_publish_release(
    release_record_id: str,
    body: dict | None = None,
    ctx: ServiceContext = Depends(get_service_context),
) -> dict:
    payload = body or {}
    return _delivery(ctx).publish_release(
        release_record_id,
        proposal_id=payload.get("proposal_id"),
        approval_message_ts=payload.get("approval_message_ts"),
    )


@router.get("/settings/integrations/github")
def get_github_settings() -> dict:
    return read_github_settings()


@router.put("/settings/integrations/github/token")
def put_github_token(body: dict) -> dict:
    return apply_github_token(token_value=body.get("token"))


@router.delete("/settings/integrations/github/token")
def delete_github_token() -> dict:
    return remove_github_token()


@router.post("/settings/integrations/github/test")
def post_github_test() -> dict:
    return probe_github_connection()
