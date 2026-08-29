"""Single-instance ProjectOS daemon (Windows-friendly)."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from projectos.clock import Clock, system_utc_now
from projectos.db import connection
from projectos.dispatch import run_dispatch
from projectos.errors import OrchestrationError
from projectos.migrate import initialize_database
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH, STATE_DIR
from projectos.recover import run_recovery
from projectos.schedule import evaluate_due
from projectos.store import utc_now_iso


@dataclass
class DaemonStatus:
    status: str
    pid: int | None
    heartbeat_at: str | None
    started_at: str | None
    last_error: str | None
    lock_path: str | None


class DaemonLock:
    """Exclusive lock file with PID; prevents duplicate daemon instances."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                old_pid = int(self.path.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                old_pid = 0
            if old_pid and _pid_alive(old_pid):
                raise OrchestrationError(
                    f"Daemon already running with pid {old_pid} (lock {self.path})"
                )
            # Stale lock
            try:
                self.path.unlink()
            except OSError:
                pass
        # Exclusive create
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(str(self.path), flags)
        except FileExistsError as exc:
            raise OrchestrationError(f"Daemon lock busy: {self.path}") from exc
        self._fh = os.fdopen(fd, "w", encoding="utf-8")
        self._fh.write(str(os.getpid()))
        self._fh.flush()

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, 0, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_lock_pid(lock_path: str | Path | None) -> int | None:
    if not lock_path:
        return None
    try:
        pid = int(Path(lock_path).read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def get_daemon_status(db_path: Path | str | None = None) -> DaemonStatus:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    initialize_database(path)
    with connection(path) as conn:
        row = conn.execute("SELECT * FROM daemon_state WHERE id = 1").fetchone()
        if row is None:
            return DaemonStatus("stopped", None, None, None, None, None)
        pid = int(row["pid"]) if row["pid"] is not None else None
        lock_path = row["lock_path"]
        status = str(row["status"])
        if pid is None and status == "running":
            pid = _read_lock_pid(lock_path)
        if pid and status == "running" and row["pid"] is None and _pid_alive(pid):
            _persist_daemon(conn, pid=pid, status="running")
        return DaemonStatus(
            status=status,
            pid=pid,
            heartbeat_at=row["heartbeat_at"],
            started_at=row["started_at"],
            last_error=row["last_error"],
            lock_path=lock_path,
        )


def _persist_daemon(conn, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE daemon_state SET {cols}, updated_at = ? WHERE id = 1",
        (*fields.values(), utc_now_iso()),
    )


def run_daemon(
    *,
    db_path: Path | str | None = None,
    registry_path: Path | str | None = None,
    poll_seconds: float = 5.0,
    max_loops: int | None = None,
    clock: Clock | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    projectctl_runner=None,
    cursor_runner=None,
    skip_identity_validation: bool = False,
    stop_flag: list[bool] | None = None,
    lock_path: Path | None = None,
) -> int:
    """Run recover → schedule → dispatch loop with single-instance lock."""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    reg = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    initialize_database(path)
    lock_file = Path(lock_path) if lock_path is not None else STATE_DIR / "projectos.daemon.lock"
    lock = DaemonLock(lock_file)
    lock.acquire()
    loops = 0
    try:
        with connection(path) as conn:
            _persist_daemon(
                conn,
                pid=os.getpid(),
                started_at=utc_now_iso(),
                heartbeat_at=utc_now_iso(),
                status="running",
                lock_path=str(lock_file),
                last_error=None,
            )
        while True:
            if stop_flag and stop_flag[0]:
                break
            try:
                # Do not hold SQLite transactions while Cursor runs.
                run_recovery(
                    db_path=path,
                    registry_path=reg,
                    projectctl_runner=projectctl_runner,
                )
                evaluate_due(
                    db_path=path,
                    registry_path=reg,
                    clock=clock or system_utc_now,
                    projectctl_runner=projectctl_runner,
                )
                run_dispatch(
                    until_idle=True,
                    db_path=path,
                    registry_path=reg,
                    cursor_runner=cursor_runner,
                    projectctl_runner=projectctl_runner,
                    skip_identity_validation=skip_identity_validation,
                    max_waves=1,
                )
                from projectos.event_dispatcher import dispatch_event_outbox

                dispatch_event_outbox(path)
                with connection(path) as conn:
                    _persist_daemon(
                        conn,
                        heartbeat_at=utc_now_iso(),
                        status="running",
                        last_error=None,
                        pid=os.getpid(),
                    )
            except OrchestrationError as exc:
                with connection(path) as conn:
                    _persist_daemon(
                        conn,
                        heartbeat_at=utc_now_iso(),
                        last_error=str(exc),
                        status="error",
                    )
                # Unrecoverable lock/identity style errors stop the daemon.
                if "already running" in str(exc).lower() or "unsafe" in str(exc).lower():
                    return 1
            except Exception as exc:  # noqa: BLE001 — bounded continue
                with connection(path) as conn:
                    _persist_daemon(
                        conn,
                        heartbeat_at=utc_now_iso(),
                        last_error=str(exc),
                        status="running",
                    )
            loops += 1
            if max_loops is not None and loops >= max_loops:
                break
            sleep_fn(poll_seconds)
        return 0
    finally:
        with connection(path) as conn:
            _persist_daemon(
                conn, status="stopped", pid=None, heartbeat_at=utc_now_iso()
            )
        lock.release()


def stop_daemon(db_path: Path | str | None = None) -> int:
    """Best-effort stop via lock/PID (Windows-appropriate)."""
    status = get_daemon_status(db_path)
    if status.pid and _pid_alive(status.pid):
        if sys.platform == "win32":
            os.system(f"taskkill /PID {status.pid} /F >NUL 2>&1")
        else:
            try:
                os.kill(status.pid, 15)
            except OSError:
                pass
    lock = STATE_DIR / "projectos.daemon.lock"
    if lock.exists():
        try:
            lock.unlink()
        except OSError:
            pass
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    initialize_database(path)
    with connection(path) as conn:
        _persist_daemon(conn, status="stopped", pid=None)
    return 0
