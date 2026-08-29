"""Shared queue/role constants for ProjectOS orchestration."""

from __future__ import annotations

VALID_QUEUES = frozenset(
    {
        "PM",
        "ARCHITECTURE",
        "DELIVERY",
        "ASSURANCE_FUNCTIONAL",
        "ASSURANCE_INTEGRATION",
        "ASSURANCE_SECURITY",
        "ASSURANCE_QUALITY",
        "QA_MANAGER",
        "RELEASE",
        "INTEGRATION",
    }
)

QUEUE_TO_ROLE = {
    "PM": "PM",
    "ARCHITECTURE": "ARCHITECTURE",
    "DELIVERY": "DELIVERY",
    "ASSURANCE_FUNCTIONAL": "ASSURANCE_FUNCTIONAL",
    "ASSURANCE_INTEGRATION": "ASSURANCE_INTEGRATION",
    "ASSURANCE_SECURITY": "ASSURANCE_SECURITY",
    "ASSURANCE_QUALITY": "ASSURANCE_QUALITY",
    "QA_MANAGER": "QA_MANAGER",
    "RELEASE": "RELEASE",
    "INTEGRATION": "INTEGRATION",
}

ASSURANCE_QUEUES = frozenset(
    {
        "ASSURANCE_FUNCTIONAL",
        "ASSURANCE_INTEGRATION",
        "ASSURANCE_SECURITY",
        "ASSURANCE_QUALITY",
    }
)

CODE_MODIFYING_ROLES = frozenset({"DELIVERY", "ARCHITECTURE", "INTEGRATION"})

ITERATION_STATES = frozenset(
    {
        "PLANNED",
        "READY",
        "RUNNING",
        "QUALITY_HOLD",
        "RELEASE_READY",
        "RELEASED",
        "BLOCKED",
        "FAILED",
        "CANCELLED",
    }
)

DEFAULT_MAX_PARALLEL = 3
