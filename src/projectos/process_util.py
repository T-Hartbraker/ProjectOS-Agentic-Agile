"""Cross-platform process helpers for operator and daemon lifecycle."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, 0, pid)
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


def kill_process_tree(pid: int, *, wait_seconds: float = 3.0) -> bool:
    """Terminate a process and its children. Returns True if no longer alive."""
    if not pid_is_alive(pid):
        return True
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if wait_seconds > 0:
        import time

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if not pid_is_alive(pid):
                return True
            time.sleep(0.1)
    return not pid_is_alive(pid)
