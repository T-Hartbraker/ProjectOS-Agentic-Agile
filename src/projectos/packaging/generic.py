"""Generic packaging adapter for explicit custom build contracts."""

from __future__ import annotations

from pathlib import Path

from projectos.delivery.contract import DeliveryContract
from projectos.errors import OrchestrationError
from projectos.packaging.registry import AdapterResult, PackagingArtifact, register_adapter


@register_adapter
class GenericPackagingAdapter:
    adapter_id = "generic"

    def detect(self, repo_root: Path) -> bool:
        return (repo_root / "setup.py").is_file() and (repo_root / "pyproject.toml").is_file()

    def validate_environment(self, repo_root: Path, contract: DeliveryContract) -> None:
        if not contract.trusted_build_command:
            raise OrchestrationError("generic adapter requires trusted_build_command in delivery.json")

    def build(self, repo_root: Path, contract: DeliveryContract, *, git_sha: str, build_dir: Path) -> None:
        build_dir.mkdir(parents=True, exist_ok=True)

    def package(
        self,
        repo_root: Path,
        contract: DeliveryContract,
        *,
        version: str,
        git_sha: str,
        build_dir: Path,
        output_dir: Path,
    ) -> AdapterResult:
        raise OrchestrationError("generic adapter package is not used in unit tests")

    def verify(self, result: AdapterResult, contract: DeliveryContract) -> None:
        if not result.ok:
            raise OrchestrationError(result.detail or "generic packaging failed")

    def collect_artifacts(self, result: AdapterResult) -> tuple[PackagingArtifact, ...]:
        return result.artifacts
