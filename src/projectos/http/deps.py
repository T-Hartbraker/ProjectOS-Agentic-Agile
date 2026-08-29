"""FastAPI dependencies. App state is bound in the factory — not from the request body."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request

from projectos.intake import IntakeService
from projectos.services import (
    ApprovalService,
    ControlService,
    MemoryAdminService,
    PlanService,
    ProjectQueryService,
    ProjectionService,
    RegistryService,
    SlackBindingService,
    ServiceContext,
)


def get_service_context(request: Request) -> ServiceContext:
    return request.app.state.service_context


def get_projectctl_runner(request: Request) -> Callable[..., Any] | None:
    return getattr(request.app.state, "projectctl_runner", None)


def get_registry_service(request: Request) -> RegistryService:
    return RegistryService(get_service_context(request))


def get_plan_service(request: Request) -> PlanService:
    return PlanService(get_service_context(request))


def get_intake_service(request: Request) -> IntakeService:
    return IntakeService(get_service_context(request))


def get_project_query_service(request: Request) -> ProjectQueryService:
    return ProjectQueryService(get_service_context(request))


def get_memory_admin_service(request: Request) -> MemoryAdminService:
    return MemoryAdminService(get_service_context(request))


def get_approval_service(request: Request) -> ApprovalService:
    return ApprovalService(get_service_context(request))


def get_slack_service(request: Request) -> SlackBindingService:
    return SlackBindingService(get_service_context(request))




def get_cursor_runner(request: Request) -> Callable[..., Any] | None:
    return getattr(request.app.state, "cursor_runner", None)


def get_projection_service(request: Request) -> ProjectionService:
    return ProjectionService(get_service_context(request))


def get_control_service(request: Request) -> ControlService:
    return ControlService(
        get_service_context(request),
        cursor_runner=get_cursor_runner(request),
        projectctl_runner=get_projectctl_runner(request),
        skip_identity_validation=bool(
            getattr(request.app.state, "skip_identity_validation", False)
        ),
    )
