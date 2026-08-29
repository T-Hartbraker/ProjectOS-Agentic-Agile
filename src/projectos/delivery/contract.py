"""Delivery contract loaded from each project's project/delivery.json."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from projectos.errors import OrchestrationError

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
INSTALLER_TEMPLATE_VARS = frozenset({"product", "version", "platform", "arch"})


@dataclass(frozen=True)
class DeliveryContract:
    schema_version: int
    delivery_type: str
    target_platforms: tuple[str, ...]
    packaging_adapter: str
    repository_provider: str
    repository_owner: str
    repository_name: str
    default_branch: str
    release_strategy: str
    installer_format: str
    installer_name_template: str
    artifact_retention: int
    code_signing_policy: str
    sbom_policy: str
    checksum_policy: str
    github_release_enabled: bool
    slack_release_announcement_enabled: bool
    product_name: str | None = None
    entry_point: str | None = None
    trusted_build_command: str | None = None
    path: Path | None = None

    @property
    def repository_slug(self) -> str:
        return f"{self.repository_owner}/{self.repository_name}"

    def render_installer_name(
        self,
        *,
        product: str,
        version: str,
        platform: str = "windows",
        arch: str = "x64",
    ) -> str:
        return self.installer_name_template.format(
            product=product,
            version=version,
            platform=platform,
            arch=arch,
        )


def delivery_json_path(repo_root: Path) -> Path:
    return Path(repo_root) / "project" / "delivery.json"


def _require_str(raw: dict[str, Any], key: str, *, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OrchestrationError(f"delivery.json missing required field {key!r} at {path}")
    return value.strip()


def _validate_installer_template(template: str, *, path: Path) -> None:
    for match in re.findall(r"\{([^}]+)\}", template):
        if match not in INSTALLER_TEMPLATE_VARS:
            raise OrchestrationError(
                f"delivery.json installer_name_template uses unsupported variable {match!r} at {path}"
            )


def _reject_untrusted_command(command: str | None, *, path: Path) -> None:
    if not command:
        return
    lowered = command.lower()
    forbidden = ("|", "&&", ";", "`", "$(", "\n", "\r", "..")
    if any(token in lowered for token in forbidden):
        raise OrchestrationError(
            f"delivery.json trusted_build_command contains forbidden shell metacharacters at {path}"
        )


def delivery_contract_missing_evidence(repo_root: Path) -> dict[str, Any]:
    """Structured blocker evidence for missing delivery.json."""
    path = delivery_json_path(Path(repo_root))
    return {
        "blocker_type": "DELIVERY_CONTRACT_MISSING",
        "path": str(path),
        "phase": "RELEASE_PREPARATION",
        "required_action": (
            "Add project/delivery.json to the repository, or authorize ProjectOS to "
            "generate a governed delivery contract draft for Sponsor review."
        ),
        "retryable": True,
        "auto_remediation": {
            "available": True,
            "approach": "infer_delivery_contract",
            "inferrable_fields": [
                "schema_version",
                "delivery_type",
                "packaging_adapter",
                "installer_name_template",
                "installer_format",
                "default_branch",
            ],
            "sponsor_decisions_required": [
                "repository_owner",
                "repository_name",
                "target_platforms",
                "code_signing_policy",
                "github_release_enabled",
            ],
            "note": (
                "ProjectOS can infer a draft contract from repository metadata but must not "
                "silently fabricate publishing destinations or signing credentials."
            ),
        },
    }


def orchestration_blocker_from_message(message: str, *, repo_root: Path | None = None) -> dict[str, Any]:
    """Map OrchestrationError text to structured blocker evidence when possible."""
    lowered = str(message or "").lower()
    if "delivery.json missing" in lowered and repo_root is not None:
        evidence = delivery_contract_missing_evidence(repo_root)
        evidence["reason"] = message[:500]
        return evidence
    return {
        "blocker_type": "ORCHESTRATION_ERROR",
        "phase": "RELEASE_PREPARATION",
        "reason": message[:500],
        "required_action": "Resolve the reported orchestration error and retry.",
        "retryable": True,
    }


def load_delivery_contract(repo_root: Path) -> DeliveryContract:
    root = Path(repo_root).resolve()
    path = delivery_json_path(root)
    if not path.is_file():
        raise OrchestrationError(
            f"delivery.json missing at {path}. Add a delivery contract before release preparation."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise OrchestrationError(f"delivery.json is malformed JSON at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise OrchestrationError(f"delivery.json must be a JSON object at {path}")

    try:
        schema_version = int(raw.get("schema_version", 0))
    except (TypeError, ValueError) as exc:
        raise OrchestrationError(f"delivery.json schema_version must be an integer at {path}") from exc
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise OrchestrationError(
            f"delivery.json unsupported schema_version {schema_version} at {path}"
        )

    platforms_raw = raw.get("target_platforms")
    if not isinstance(platforms_raw, list) or not platforms_raw:
        raise OrchestrationError(f"delivery.json target_platforms must be a non-empty array at {path}")
    platforms = tuple(str(item).strip() for item in platforms_raw if str(item).strip())
    if not platforms:
        raise OrchestrationError(f"delivery.json target_platforms is empty at {path}")

    owner = _require_str(raw, "repository_owner", path=path)
    name = _require_str(raw, "repository_name", path=path)
    if not SAFE_ID_RE.match(owner) or not SAFE_ID_RE.match(name):
        raise OrchestrationError(f"delivery.json repository owner/name are invalid at {path}")

    template = _require_str(raw, "installer_name_template", path=path)
    _validate_installer_template(template, path=path)
    trusted_build = raw.get("trusted_build_command")
    trusted_text = str(trusted_build).strip() if trusted_build else None
    _reject_untrusted_command(trusted_text, path=path)

    return DeliveryContract(
        schema_version=schema_version,
        delivery_type=_require_str(raw, "delivery_type", path=path),
        target_platforms=platforms,
        packaging_adapter=_require_str(raw, "packaging_adapter", path=path),
        repository_provider=_require_str(raw, "repository_provider", path=path),
        repository_owner=owner,
        repository_name=name,
        default_branch=_require_str(raw, "default_branch", path=path),
        release_strategy=_require_str(raw, "release_strategy", path=path),
        installer_format=_require_str(raw, "installer_format", path=path),
        installer_name_template=template,
        artifact_retention=int(raw.get("artifact_retention") or 10),
        code_signing_policy=_require_str(raw, "code_signing_policy", path=path),
        sbom_policy=_require_str(raw, "sbom_policy", path=path),
        checksum_policy=_require_str(raw, "checksum_policy", path=path),
        github_release_enabled=bool(raw.get("github_release_enabled")),
        slack_release_announcement_enabled=bool(raw.get("slack_release_announcement_enabled")),
        product_name=str(raw.get("product_name") or "").strip() or None,
        entry_point=str(raw.get("entry_point") or "").strip() or None,
        trusted_build_command=trusted_text,
        path=path,
    )


def infer_delivery_contract(
    *,
    product_name: str,
    repository_owner: str,
    repository_name: str,
    target_platforms: list[str] | None = None,
    external_distribution: bool = True,
) -> dict[str, Any]:
    """Infer a sane default contract for Sponsor review during project creation."""
    platforms = target_platforms or ["windows-x64"]
    signing = "required_for_production" if external_distribution else "not_required"
    return {
        "schema_version": 1,
        "delivery_type": "desktop_application",
        "target_platforms": platforms,
        "packaging_adapter": "auto",
        "repository_provider": "github",
        "repository_owner": repository_owner,
        "repository_name": repository_name,
        "default_branch": "main",
        "release_strategy": "semantic_version",
        "installer_format": "exe",
        "installer_name_template": "{product}-Setup-{version}.exe",
        "artifact_retention": 10,
        "code_signing_policy": signing,
        "sbom_policy": "required",
        "checksum_policy": "sha256",
        "github_release_enabled": True,
        "slack_release_announcement_enabled": True,
        "product_name": product_name,
    }
