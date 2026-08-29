"""Canonical worker execution status — normalize comparisons across the control plane."""

from __future__ import annotations

from enum import Enum


class WorkerExecutionStatus(str, Enum):
  SUCCEEDED = "succeeded"
  FAILED = "failed"
  BLOCKED = "blocked"
  TIMED_OUT = "timed_out"
  CANCELLED = "cancelled"
  IDLE = "idle"
  SKIPPED = "skipped"
  ERROR = "error"
  LEASE_FAILED = "lease_failed"


_TERMINAL_FAILURE = frozenset(
    {
        WorkerExecutionStatus.FAILED.value,
        WorkerExecutionStatus.ERROR.value,
        WorkerExecutionStatus.BLOCKED.value,
        WorkerExecutionStatus.TIMED_OUT.value,
        WorkerExecutionStatus.CANCELLED.value,
        WorkerExecutionStatus.LEASE_FAILED.value,
    }
)


def normalize_worker_status(status: str | None) -> str:
    return str(status or "").strip().lower()


def worker_succeeded(status: str | None) -> bool:
    return normalize_worker_status(status) == WorkerExecutionStatus.SUCCEEDED.value


def worker_failed(status: str | None) -> bool:
    return normalize_worker_status(status) in _TERMINAL_FAILURE


def worker_skipped_or_idle(status: str | None) -> bool:
    normalized = normalize_worker_status(status)
    return normalized in {
        WorkerExecutionStatus.IDLE.value,
        WorkerExecutionStatus.SKIPPED.value,
    }
