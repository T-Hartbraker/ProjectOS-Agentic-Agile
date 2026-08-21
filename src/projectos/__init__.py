"""ProjectOS — multi-project orchestration above delivery repositories."""

from projectos.registry import ProjectRegistry, RegistryEntry, load_registry
from projectos.validation import (
    ValidatedProject,
    ValidationReport,
    validate_registry,
    validate_registry_entry,
)

__all__ = [
    "ProjectRegistry",
    "RegistryEntry",
    "ValidatedProject",
    "ValidationReport",
    "load_registry",
    "validate_registry",
    "validate_registry_entry",
]

__version__ = "0.1.0"
