"""Packaging adapter interface and registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from projectos.delivery.contract import DeliveryContract
from projectos.errors import OrchestrationError


@dataclass(frozen=True)
class PackagingArtifact:
    artifact_name: str
    artifact_type: str
    platform: str
    architecture: str
    local_path: Path
    sha256: str
    size_bytes: int
    signature_status: str = "not_configured"
    signature_identity: str | None = None


@dataclass(frozen=True)
class AdapterResult:
    ok: bool
    artifacts: tuple[PackagingArtifact, ...]
    sbom_path: Path | None = None
    detail: str = ""
    logs: tuple[str, ...] = ()


class PackagingAdapter(Protocol):
    adapter_id: str

    def detect(self, repo_root: Path) -> bool: ...

    def validate_environment(self, repo_root: Path, contract: DeliveryContract) -> None: ...

    def build(self, repo_root: Path, contract: DeliveryContract, *, git_sha: str, build_dir: Path) -> None: ...

    def package(
        self,
        repo_root: Path,
        contract: DeliveryContract,
        *,
        version: str,
        git_sha: str,
        build_dir: Path,
        output_dir: Path,
    ) -> AdapterResult: ...

    def verify(self, result: AdapterResult, contract: DeliveryContract) -> None: ...

    def collect_artifacts(self, result: AdapterResult) -> tuple[PackagingArtifact, ...]: ...


_ADAPTERS: dict[str, type] = {}


def register_adapter(adapter_cls: type) -> type:
    instance = adapter_cls()
    _ADAPTERS[instance.adapter_id] = adapter_cls
    return adapter_cls


def get_adapter(adapter_id: str):
    if adapter_id not in _ADAPTERS:
        from projectos.packaging.python_desktop import PythonDesktopAdapter

        _ = PythonDesktopAdapter
    if adapter_id not in _ADAPTERS:
        raise OrchestrationError(f"Unknown packaging adapter: {adapter_id!r}")
    return _ADAPTERS[adapter_id]()


def detect_packaging_adapter(repo_root: Path, contract: DeliveryContract) -> str:
    requested = contract.packaging_adapter
    if requested != "auto":
        adapter = get_adapter(requested)
        if not adapter.detect(repo_root):
            raise OrchestrationError(
                f"Configured packaging adapter {requested!r} does not match project at {repo_root}"
            )
        return requested

    matches: list[str] = []
    for adapter_id in sorted(_ADAPTERS):
        adapter = get_adapter(adapter_id)
        if adapter.detect(repo_root):
            matches.append(adapter_id)
    if not matches:
        from projectos.packaging.python_desktop import PythonDesktopAdapter

        if PythonDesktopAdapter().detect(repo_root):
            matches.append("python_desktop")
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise OrchestrationError(
            "Could not detect a packaging adapter. Configure packaging_adapter explicitly in delivery.json."
        )
    raise OrchestrationError(
        f"Ambiguous packaging adapters detected: {', '.join(matches)}. Configure packaging_adapter explicitly."
    )
