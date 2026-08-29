"""Project registry routes. Paths are project_human_id, never raw filesystem browse."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, status

from projectos.http.deps import get_projectctl_runner, get_registry_service
from projectos.http.schemas import (
    OnboardingResponse,
    ProjectListResponse,
    ProjectResponse,
    RegisterProjectRequest,
)
from projectos.registry import RegistryEntry
from projectos.services import RegistryService


router = APIRouter(prefix="/v1/projects", tags=["projects"])


def _project_response(entry: RegistryEntry) -> ProjectResponse:
    return ProjectResponse(
        project_human_id=entry.project_human_id,
        repository_root=str(Path(entry.repository_root)),
        enabled=bool(entry.enabled),
    )


def _onboarding_response(result) -> OnboardingResponse:
    return OnboardingResponse(
        action=result.action,
        project_human_id=result.entry.project_human_id,
        repository_root=str(Path(result.entry.repository_root)),
        enabled=bool(result.entry.enabled),
        git_root=str(Path(result.git_root)),
        project_name=result.identity.project_name,
    )


@router.get("", response_model=ProjectListResponse)
def list_projects(
    svc: RegistryService = Depends(get_registry_service),
) -> ProjectListResponse:
    return ProjectListResponse(projects=[_project_response(e) for e in svc.list_projects()])


@router.get("/{project_human_id}", response_model=ProjectResponse)
def get_project(
    project_human_id: str,
    svc: RegistryService = Depends(get_registry_service),
) -> ProjectResponse:
    return _project_response(svc.show(project_human_id))


@router.post("", response_model=OnboardingResponse, status_code=status.HTTP_201_CREATED)
def register_project_route(
    body: RegisterProjectRequest,
    svc: RegistryService = Depends(get_registry_service),
    projectctl_runner=Depends(get_projectctl_runner),
) -> OnboardingResponse:
    result = svc.register(
        body.repository_path,
        projectctl_runner=projectctl_runner,
    )
    return _onboarding_response(result)


@router.post("/{project_human_id}/disable", response_model=OnboardingResponse)
def disable_project_route(
    project_human_id: str,
    svc: RegistryService = Depends(get_registry_service),
) -> OnboardingResponse:
    return _onboarding_response(svc.disable(project_human_id))
