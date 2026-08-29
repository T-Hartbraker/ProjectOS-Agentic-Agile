"""Build executor abstraction (LOCAL vs CI)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from projectos.errors import OrchestrationError


class BuildExecutorKind(str, Enum):
    LOCAL = "LOCAL"
    CI = "CI"


@dataclass(frozen=True)
class BuildExecutorResult:
    ok: bool
    build_id: str
    executor: str
    detail: str = ""
    evidence: dict[str, Any] | None = None


class BuildExecutor(Protocol):
    kind: BuildExecutorKind

    def run_release_build(
        self,
        *,
        repository: str,
        git_sha: str,
        version: str,
        workflow_inputs: dict[str, Any],
    ) -> BuildExecutorResult: ...


class LocalBuildExecutor:
    kind = BuildExecutorKind.LOCAL

    def __init__(self, *, runner: Callable[..., BuildExecutorResult] | None = None) -> None:
        self._runner = runner

    def run_release_build(
        self,
        *,
        repository: str,
        git_sha: str,
        version: str,
        workflow_inputs: dict[str, Any],
    ) -> BuildExecutorResult:
        if self._runner is not None:
            return self._runner(
                repository=repository,
                git_sha=git_sha,
                version=version,
                workflow_inputs=workflow_inputs,
            )
        build_id = str(workflow_inputs.get("build_id") or "")
        return BuildExecutorResult(
            ok=True,
            build_id=build_id,
            executor=self.kind.value,
            detail="local build executor acknowledged",
            evidence={"git_sha": git_sha, "version": version},
        )


class GitHubActionsBuildExecutor:
    kind = BuildExecutorKind.CI

    def __init__(self, *, trigger_workflow: Callable[..., dict[str, Any]] | None = None) -> None:
        self._trigger = trigger_workflow

    def run_release_build(
        self,
        *,
        repository: str,
        git_sha: str,
        version: str,
        workflow_inputs: dict[str, Any],
    ) -> BuildExecutorResult:
        if self._trigger is None:
            raise OrchestrationError("GitHub Actions executor is not configured")
        payload = self._trigger(
            repository=repository,
            git_sha=git_sha,
            version=version,
            inputs=workflow_inputs,
        )
        if not payload.get("ok"):
            raise OrchestrationError(str(payload.get("detail") or "GitHub Actions workflow failed"))
        return BuildExecutorResult(
            ok=True,
            build_id=str(payload.get("build_id") or workflow_inputs.get("build_id") or ""),
            executor=self.kind.value,
            detail=str(payload.get("detail") or "workflow triggered"),
            evidence=payload,
        )


def select_build_executor(*, prefer_ci: bool, ci_available: bool) -> BuildExecutor:
    if prefer_ci and ci_available:
        return GitHubActionsBuildExecutor()
    return LocalBuildExecutor()
