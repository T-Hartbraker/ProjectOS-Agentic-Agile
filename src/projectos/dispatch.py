"""Bounded parallel dispatch over durable READY jobs."""

from __future__ import annotations

import signal
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from projectos.constants import DEFAULT_MAX_PARALLEL
from projectos.cursor_adapter import cancel_active_cursor_processes
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.store import list_eligible_ready_jobs, recover_expired_leases
from projectos.worker import WorkerResult, run_once


@dataclass
class DispatchResult:
    mode: str
    completed: list[WorkerResult] = field(default_factory=list)
    message: str = ""
    cancelled: bool = False

    @property
    def exit_code(self) -> int:
        if self.cancelled:
            return 130
        if any(
            r.exit_code != 0 and r.status not in {"idle", "skipped"}
            for r in self.completed
        ):
            return 1
        return 0


def _run_one_job(
    *,
    job_human_id: str,
    db_path: Path,
    registry_path: Path,
    lease_seconds: int,
    timeout_seconds: float,
    cursor_runner: Callable[..., Any] | None,
    projectctl_runner,
    skip_identity_validation: bool,
    cancel_event: threading.Event,
) -> WorkerResult:
    return run_once(
        db_path=db_path,
        registry_path=registry_path,
        job_human_id=job_human_id,
        lease_seconds=lease_seconds,
        timeout_seconds=timeout_seconds,
        cursor_runner=cursor_runner,
        projectctl_runner=projectctl_runner,
        skip_identity_validation=skip_identity_validation,
        worker_id=f"dispatch:{job_human_id}:{threading.get_ident()}",
        cancel_event=cancel_event,
    )


def _drain_in_flight(
    in_flight: dict[Any, str],
    results: list[WorkerResult],
    *,
    grace_seconds: float,
) -> None:
    deadline = time.time() + max(0.0, grace_seconds)
    while in_flight and time.time() < deadline:
        remaining = max(0.01, deadline - time.time())
        done, _ = wait(
            list(in_flight.keys()),
            timeout=min(0.5, remaining),
            return_when=FIRST_COMPLETED,
        )
        for fut in done:
            in_flight.pop(fut, None)
            try:
                results.append(fut.result(timeout=0))
            except Exception as exc:  # noqa: BLE001
                results.append(
                    WorkerResult(
                        status="error",
                        job_human_id=None,
                        message=str(exc),
                        exit_code=1,
                    )
                )
    # Drop remaining futures without blocking forever.
    for fut in list(in_flight.keys()):
        in_flight.pop(fut, None)
        fut.cancel()


def run_dispatch(
    *,
    once: bool = False,
    until_idle: bool = False,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    db_path: Path | str | None = None,
    registry_path: Path | str | None = None,
    lease_seconds: int = 900,
    timeout_seconds: float = 1800.0,
    cursor_runner: Callable[..., Any] | None = None,
    projectctl_runner=None,
    skip_identity_validation: bool = False,
    idle_poll_seconds: float = 0.05,
    max_waves: int | None = None,
    cancel_event: threading.Event | None = None,
    wait_cycle_seconds: float = 1.0,
    shutdown_grace_seconds: float = 5.0,
) -> DispatchResult:
    if not once and not until_idle:
        once = True
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    reg = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    initialize_database(path)
    max_parallel = max(1, int(max_parallel))
    results: list[WorkerResult] = []
    waves = 0
    stop = False
    cancelled = False
    local_cancel = cancel_event or threading.Event()

    def _request_cancel(*_args) -> None:
        nonlocal cancelled, stop
        cancelled = True
        stop = True
        local_cancel.set()
        cancel_active_cursor_processes(grace_seconds=min(2.0, shutdown_grace_seconds))

    previous_handler = None
    try:
        previous_handler = signal.signal(signal.SIGINT, _request_cancel)
    except (ValueError, OSError):
        # Not in main thread — caller can pass cancel_event.
        previous_handler = None

    pool = ThreadPoolExecutor(max_workers=max_parallel)
    in_flight: dict[Any, str] = {}
    try:
        while not stop and not local_cancel.is_set():
            with connection(path) as conn:
                recover_expired_leases(conn)
                busy = set(in_flight.values())
                slots = max_parallel - len(in_flight)
                selected = []
                if slots > 0 and not local_cancel.is_set():
                    for job in list_eligible_ready_jobs(conn, limit=slots + len(busy)):
                        if job.human_id in busy:
                            continue
                        selected.append(job)
                        if len(selected) >= slots:
                            break

            for job in selected:
                if local_cancel.is_set():
                    break
                fut = pool.submit(
                    _run_one_job,
                    job_human_id=job.human_id,
                    db_path=path,
                    registry_path=reg,
                    lease_seconds=lease_seconds,
                    timeout_seconds=timeout_seconds,
                    cursor_runner=cursor_runner,
                    projectctl_runner=projectctl_runner,
                    skip_identity_validation=skip_identity_validation,
                    cancel_event=local_cancel,
                )
                in_flight[fut] = job.human_id

            if not in_flight:
                stop = True
                break

            done, _ = wait(
                list(in_flight.keys()),
                timeout=wait_cycle_seconds,
                return_when=FIRST_COMPLETED,
            )
            for fut in done:
                in_flight.pop(fut, None)
                try:
                    results.append(fut.result(timeout=0))
                except Exception as exc:  # noqa: BLE001
                    results.append(
                        WorkerResult(
                            status="error",
                            job_human_id=None,
                            message=str(exc),
                            exit_code=1,
                        )
                    )

            if local_cancel.is_set():
                stop = True
                break

            waves += 1
            if once:
                _drain_in_flight(
                    in_flight, results, grace_seconds=shutdown_grace_seconds
                )
                stop = True
            elif max_waves is not None and waves >= max_waves:
                _drain_in_flight(
                    in_flight, results, grace_seconds=shutdown_grace_seconds
                )
                stop = True
            elif until_idle:
                if not in_flight:
                    time.sleep(idle_poll_seconds)
                    with connection(path) as conn:
                        if not list_eligible_ready_jobs(conn, limit=1):
                            stop = True
    except KeyboardInterrupt:
        _request_cancel()
    finally:
        if local_cancel.is_set() or cancelled:
            cancel_active_cursor_processes(
                grace_seconds=min(2.0, shutdown_grace_seconds)
            )
            _drain_in_flight(in_flight, results, grace_seconds=shutdown_grace_seconds)
        pool.shutdown(wait=False, cancel_futures=True)
        if previous_handler is not None:
            try:
                signal.signal(signal.SIGINT, previous_handler)
            except (ValueError, OSError):
                pass

    mode = "once" if once else "until-idle"
    msg = f"dispatch {mode}: completed {len(results)} job run(s)"
    if cancelled or local_cancel.is_set():
        msg += " (cancelled)"
    return DispatchResult(
        mode=mode,
        completed=results,
        message=msg,
        cancelled=cancelled or local_cancel.is_set(),
    )
