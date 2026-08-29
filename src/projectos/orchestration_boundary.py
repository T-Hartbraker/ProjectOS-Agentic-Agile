"""Narrow orchestration exception boundaries with internal defect routing."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any, TypeVar

from projectos.domain_events import EventContext
from projectos.errors import OrchestrationError
from projectos.internal_defect import handle_internal_defect

T = TypeVar("T")

_INTERNAL_DEFECT_EXCEPTIONS = (AttributeError, TypeError, KeyError)


def _is_likely_internal_defect(exc: Exception) -> bool:
    if isinstance(exc, _INTERNAL_DEFECT_EXCEPTIONS):
        return True
    message = str(exc).lower()
    return "has no attribute" in message or "not subscriptable" in message


def run_with_internal_defect_routing(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext | None,
    project_id: str,
    component: str,
    operation: str,
    fn: Callable[[], T],
    in_project_scope: bool = False,
    service_ctx=None,
    worker=None,
    repository_root: str | None = None,
) -> T:
    """Execute orchestration work and route internal ProjectOS defects to PM policy."""
    try:
        return fn()
    except OrchestrationError:
        raise
    except Exception as exc:
        if event_ctx is None or not _is_likely_internal_defect(exc):
            raise
        handle_internal_defect(
            conn,
            event_ctx=event_ctx,
            error=exc,
            component=component,
            operation=operation,
            project_id=project_id,
            in_project_scope=in_project_scope,
            service_ctx=service_ctx,
            worker=worker,
            repository_root=repository_root,
        )
        raise OrchestrationError(
            f"Internal defect in {component}.{operation}: {type(exc).__name__}: {exc}"
        ) from exc
