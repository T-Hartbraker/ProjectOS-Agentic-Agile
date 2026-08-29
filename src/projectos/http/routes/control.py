"""Daemon and scheduler status. No start/stop via shell, no workspace paths."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from projectos.http.deps import get_control_service
from projectos.http.schemas import DaemonStatusResponse, SchedulerStatusResponse
from projectos.services import ControlService

router = APIRouter(prefix="/v1", tags=["control"])


@router.get("/daemon", response_model=DaemonStatusResponse)
def daemon_status(
    svc: ControlService = Depends(get_control_service),
) -> DaemonStatusResponse:
    return DaemonStatusResponse(**svc.daemon_status())


@router.get("/scheduler", response_model=SchedulerStatusResponse)
def scheduler_status(
    svc: ControlService = Depends(get_control_service),
) -> SchedulerStatusResponse:
    payload = svc.scheduler_status()
    return SchedulerStatusResponse(
        daemon=DaemonStatusResponse(**payload["daemon"]),
        schedules=payload["schedules"],
    )
