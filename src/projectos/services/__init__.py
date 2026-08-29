"""Python application services for ProjectOS operator capabilities.

CLI and a future HTTP adapter should call these façades rather than
reimplementing orchestration policy.
"""

from projectos.project_context import ProjectContext, resolve_project_context
from projectos.services.context import ServiceContext
from projectos.services.control import ControlService, IdempotentResult
from projectos.services.projection import ProjectionService, ProjectProjection
from projectos.services.facades import (
    AgentRunLearningRecord,
    ApprovalService,
    DaemonService,
    DispatchService,
    IterationService,
    JobLearningRecord,
    LearningService,
    MemoryAdminService,
    PlanService,
    ProjectQueryService,
    ProjectSummary,
    CurrentIterationRelease,
    ReclaimRunningResult,
    RecoverService,
    RegistryService,
    ReleaseService,
    ReportingService,
    SlackBindingService,
    StatusService,
    WorkerService,
)

__all__ = [
    "AgentRunLearningRecord",
    "ApprovalService",
    "ControlService",
    "DaemonService",
    "IdempotentResult",
    "DispatchService",
    "IterationService",
    "JobLearningRecord",
    "LearningService",
    "MemoryAdminService",
    "PlanService",
    "ProjectQueryService",
    "ProjectionService",
    "ProjectProjection",
    "ProjectSummary",
    "CurrentIterationRelease",
    "ProjectContext",
    "ReclaimRunningResult",
    "RecoverService",
    "RegistryService",
    "ReleaseService",
    "ReportingService",
    "SlackBindingService",
    "ServiceContext",
    "resolve_project_context",
    "StatusService",
    "WorkerService",
]
