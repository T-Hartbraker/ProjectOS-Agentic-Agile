"""Authoritative defaults for governed new-project bootstrap."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from projectos.errors import OrchestrationError
from projectos.paths import PROJECTOS_ROOT

DEFAULT_PROJECT_DEFAULTS_PATH = PROJECTOS_ROOT / "config" / "project_defaults.json"
DEFAULT_DELIVERY_TEMPLATE_ROOT = PROJECTOS_ROOT / "templates" / "delivery-project"


@dataclass(frozen=True)
class ProjectDefaults:
    projects_root: Path
    delivery_template_root: Path


def _resolve_path(value: str | None, *, label: str) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        raise OrchestrationError(f"{label} must be an absolute path (got {text!r})")
    return path.resolve()


def load_project_defaults(path: Path | str | None = None) -> ProjectDefaults:
    """Load projects_root and delivery template root from config and env."""
    env_root = os.environ.get("PROJECTOS_PROJECTS_ROOT", "").strip()
    if env_root:
        projects_root = _resolve_path(env_root, label="PROJECTOS_PROJECTS_ROOT")
    else:
        projects_root = None
        target = Path(path) if path is not None else DEFAULT_PROJECT_DEFAULTS_PATH
        if target.is_file():
            raw = json.loads(target.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise OrchestrationError(f"project defaults must be a JSON object at {target}")
            projects_root = _resolve_path(raw.get("projects_root"), label="projects_root")
            template_override = _resolve_path(
                raw.get("delivery_template_root"), label="delivery_template_root"
            )
        else:
            template_override = None
        if projects_root is None:
            raise OrchestrationError(
                "projects_root is not configured. Set PROJECTOS_PROJECTS_ROOT or "
                f"add projects_root to {DEFAULT_PROJECT_DEFAULTS_PATH}"
            )
        delivery_template_root = template_override or DEFAULT_DELIVERY_TEMPLATE_ROOT
        return ProjectDefaults(
            projects_root=projects_root,
            delivery_template_root=delivery_template_root,
        )

    template_override = None
    target = Path(path) if path is not None else DEFAULT_PROJECT_DEFAULTS_PATH
    if target.is_file():
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            template_override = _resolve_path(
                raw.get("delivery_template_root"), label="delivery_template_root"
            )
    delivery_template_root = template_override or DEFAULT_DELIVERY_TEMPLATE_ROOT
    return ProjectDefaults(
        projects_root=projects_root,
        delivery_template_root=delivery_template_root,
    )
