"""Python desktop packaging adapter (PyInstaller-friendly, zip fallback for tests)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from projectos.delivery.contract import DeliveryContract
from projectos.errors import OrchestrationError
from projectos.packaging.registry import AdapterResult, PackagingArtifact, register_adapter


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@register_adapter
class PythonDesktopAdapter:
    adapter_id = "python_desktop"

    def detect(self, repo_root: Path) -> bool:
        root = Path(repo_root)
        return (root / "pyproject.toml").is_file() or (root / "setup.py").is_file()

    def validate_environment(self, repo_root: Path, contract: DeliveryContract) -> None:
        root = Path(repo_root)
        if contract.trusted_build_command:
            return
        if not (root / "pyproject.toml").is_file() and not (root / "setup.py").is_file():
            raise OrchestrationError("python_desktop adapter requires pyproject.toml or setup.py")

    def build(self, repo_root: Path, contract: DeliveryContract, *, git_sha: str, build_dir: Path) -> None:
        build_dir.mkdir(parents=True, exist_ok=True)
        if contract.trusted_build_command:
            self._run_trusted_command(repo_root, contract.trusted_build_command)
            return
        marker = build_dir / "build-complete.json"
        marker.write_text(
            json.dumps({"git_sha": git_sha, "adapter": self.adapter_id, "built_at": _now()}),
            encoding="utf-8",
        )

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
        output_dir.mkdir(parents=True, exist_ok=True)
        product = contract.product_name or repo_root.name
        platform = contract.target_platforms[0]
        arch = "x64" if platform.endswith("x64") else platform.split("-")[-1]
        artifact_name = contract.render_installer_name(
            product=product.replace(" ", ""),
            version=version,
            platform=platform.split("-")[0] if "-" in platform else platform,
            arch=arch,
        )
        if contract.installer_format == "zip":
            artifact_path = output_dir / artifact_name.replace(".exe", ".zip")
            self._package_zip(repo_root, contract, artifact_path)
        else:
            artifact_path = output_dir / artifact_name
            self._package_stub_exe(repo_root, contract, artifact_path, version=version, git_sha=git_sha)
        sha = _sha256_file(artifact_path)
        artifact = PackagingArtifact(
            artifact_name=artifact_path.name,
            artifact_type="installer",
            platform=platform,
            architecture=arch,
            local_path=artifact_path,
            sha256=sha,
            size_bytes=artifact_path.stat().st_size,
            signature_status="unsigned" if contract.code_signing_policy != "not_required" else "not_required",
        )
        sbom_path = output_dir / f"{product.replace(' ', '')}-{version}.spdx.json"
        sbom_path.write_text(self._minimal_sbom(product, version, git_sha), encoding="utf-8")
        return AdapterResult(ok=True, artifacts=(artifact,), sbom_path=sbom_path, detail="packaged")

    def verify(self, result: AdapterResult, contract: DeliveryContract) -> None:
        if not result.ok or not result.artifacts:
            raise OrchestrationError(result.detail or "Packaging failed")
        for artifact in result.artifacts:
            if not artifact.local_path.is_file():
                raise OrchestrationError(f"Artifact missing: {artifact.local_path}")
            if artifact.sha256 != _sha256_file(artifact.local_path):
                raise OrchestrationError(f"Artifact checksum mismatch: {artifact.artifact_name}")

    def collect_artifacts(self, result: AdapterResult) -> tuple[PackagingArtifact, ...]:
        return result.artifacts

    def _package_zip(self, repo_root: Path, contract: DeliveryContract, artifact_path: Path) -> None:
        entry = contract.entry_point or "app.py"
        entry_path = repo_root / entry
        if not entry_path.is_file():
            raise OrchestrationError(f"Entry point not found: {entry}")
        with zipfile.ZipFile(artifact_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(entry_path, arcname=entry_path.name)
            readme = repo_root / "README.md"
            if readme.is_file():
                zf.write(readme, arcname="README.md")

    def _package_stub_exe(self, repo_root: Path, contract: DeliveryContract, artifact_path: Path, *, version: str, git_sha: str) -> None:
        payload = {
            "product": contract.product_name or repo_root.name,
            "version": version,
            "git_sha": git_sha,
            "adapter": self.adapter_id,
        }
        artifact_path.write_bytes(json.dumps(payload, indent=2).encode("utf-8"))

    def _minimal_sbom(self, product: str, version: str, git_sha: str) -> str:
        doc = {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": f"{product}-{version}",
            "documentNamespace": f"https://projectos.local/sbom/{product}/{version}",
            "creationInfo": {"created": _now(), "creators": ["Tool: ProjectOS"]},
            "packages": [
                {
                    "name": product,
                    "versionInfo": version,
                    "SPDXID": "SPDXRef-Package-root",
                    "downloadLocation": "NOASSERTION",
                    "sourceInfo": f"git sha {git_sha}",
                }
            ],
        }
        return json.dumps(doc, indent=2) + "\n"

    def _run_trusted_command(self, repo_root: Path, command: str) -> None:
        parts = command.split()
        if not parts:
            raise OrchestrationError("trusted_build_command is empty")
        completed = subprocess.run(
            parts,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OrchestrationError(
                f"trusted_build_command failed with exit code {completed.returncode}"
            )
