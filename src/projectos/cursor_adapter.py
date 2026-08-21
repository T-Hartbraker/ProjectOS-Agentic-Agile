"""Centralized Cursor Agent CLI adapter."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from projectos.errors import CursorAdapterError
from projectos.paths import RUN_OUTPUT_DIR

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)CURSOR_API_KEY=\S+"),
]

_ACTIVE_LOCK = threading.Lock()
_ACTIVE_PROCS: dict[int, subprocess.Popen[str]] = {}

# Unattended autonomous execution defaults (agent --help: --force / --yolo).
DEFAULT_UNATTENDED_FORCE = True
DEFAULT_UNATTENDED_TRUST = True
POLL_INTERVAL_SECONDS = 0.1
PIPE_DRAIN_SECONDS = 3.0
TREE_KILL_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class CursorRunResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    started_at: str
    ended_at: str
    duration_ms: int
    output_ref: str
    stdout_ref: str
    stderr_ref: str
    prompt_ref: str | None
    workspace: Path
    worktree_name: str | None
    usage: dict[str, Any] | None
    timed_out: bool = False
    cancelled: bool = False


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def resolve_cursor_agent_bin(
    explicit: str | Path | None = None,
) -> str:
    if explicit is not None:
        return str(explicit)
    env = os.environ.get("CURSOR_AGENT_BIN") or os.environ.get("PROJECTOS_CURSOR_BIN")
    if env:
        return env
    for name in ("agent", "cursor-agent", "agent.cmd", "cursor-agent.cmd"):
        found = shutil.which(name)
        if found:
            return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent" / "agent.cmd"
    if local.is_file():
        return str(local)
    raise CursorAdapterError(
        "Cursor Agent CLI not found. Set CURSOR_AGENT_BIN or install cursor-agent."
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def build_cursor_command(
    *,
    agent_bin: str,
    prompt: str,
    workspace: Path,
    mode: str | None = None,
    output_format: str = "text",
    worktree_name: str | None = None,
    trust: bool = DEFAULT_UNATTENDED_TRUST,
    force: bool = DEFAULT_UNATTENDED_FORCE,
    use_yolo_alias: bool = False,
) -> list[str]:
    """Build non-interactive Cursor Agent argv.

    Prefer ``--force`` (``--yolo`` is documented as an alias). Always use
    ``--print`` for scripted execution. Never use interactive / ask mode for
    autonomous delivery work.
    """
    if mode == "ask":
        raise CursorAdapterError(
            "Refusing --mode=ask for ProjectOS autonomous execution"
        )
    cmd = [agent_bin, "--print", "--output-format", output_format]
    if trust:
        cmd.append("--trust")
    if force:
        cmd.append("--yolo" if use_yolo_alias else "--force")
    if mode:
        cmd.extend(["--mode", mode])
    cmd.extend(["--workspace", str(workspace)])
    if worktree_name:
        cmd.extend(["--worktree", worktree_name])
    cmd.append(prompt)
    return cmd


def register_active_process(proc: subprocess.Popen[str]) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS[id(proc)] = proc


def unregister_active_process(proc: subprocess.Popen[str]) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.pop(id(proc), None)


def terminate_process_tree(pid: int, *, grace_seconds: float = TREE_KILL_GRACE_SECONDS) -> None:
    """Terminate only the process tree rooted at ``pid`` (not the Cursor editor)."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        # /T = tree; /F = force. Targets only this PID lineage.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
                timeout=max(1.0, grace_seconds),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    deadline = time.time() + max(0.1, grace_seconds)
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def cancel_active_cursor_processes(*, grace_seconds: float = 2.0) -> int:
    """Terminate active ProjectOS-spawned Cursor subprocess trees."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE_PROCS.values())
    for proc in procs:
        try:
            if proc.poll() is None and proc.pid:
                terminate_process_tree(proc.pid, grace_seconds=grace_seconds)
        except OSError:
            pass
    deadline = time.time() + max(0.0, grace_seconds)
    for proc in procs:
        remaining = max(0.01, deadline - time.time())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                if proc.pid:
                    terminate_process_tree(proc.pid, grace_seconds=1.0)
            except OSError:
                pass
        unregister_active_process(proc)
    return len(procs)


def invoke_cursor_agent(
    *,
    prompt: str,
    workspace: Path,
    run_id: str,
    mode: str | None = None,
    output_format: str = "text",
    worktree_name: str | None = None,
    timeout_seconds: float | None = 1800.0,
    agent_bin: str | Path | None = None,
    output_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    trust: bool = DEFAULT_UNATTENDED_TRUST,
    force: bool = DEFAULT_UNATTENDED_FORCE,
    cancel_event: threading.Event | None = None,
) -> CursorRunResult:
    """Invoke Cursor Agent via subprocess argument arrays (never shell strings)."""
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise CursorAdapterError(f"Workspace does not exist: {workspace}")

    bin_path = resolve_cursor_agent_bin(agent_bin)
    command = build_cursor_command(
        agent_bin=bin_path,
        prompt=prompt,
        workspace=workspace,
        mode=mode,
        output_format=output_format,
        worktree_name=worktree_name,
        trust=trust,
        force=force,
    )

    out_root = Path(output_dir) if output_dir is not None else RUN_OUTPUT_DIR / run_id
    out_root.mkdir(parents=True, exist_ok=True)
    prompt_path = out_root / "prompt.txt"
    stdout_path = out_root / "stdout.txt"
    stderr_path = out_root / "stderr.txt"
    meta_path = out_root / "meta.json"
    prompt_path.write_text(prompt, encoding="utf-8")

    started = time.perf_counter()
    started_at = _iso_now()

    if runner is not None:
        stdout, stderr, returncode, timed_out, cancelled = _run_via_callable(
            runner,
            command,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
    else:
        stdout, stderr, returncode, timed_out, cancelled = _run_via_popen(
            command,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )

    ended_at = _iso_now()
    duration_ms = int((time.perf_counter() - started) * 1000)
    stdout = redact_secrets(stdout)
    stderr = redact_secrets(stderr)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")

    usage: dict[str, Any] | None = None
    if output_format == "json":
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict) and "usage" in parsed:
                usage = parsed.get("usage")  # type: ignore[assignment]
        except json.JSONDecodeError:
            usage = {"status": "unknown"}
    else:
        usage = {"status": "unknown"}

    meta = {
        "command": command,
        "returncode": returncode,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "workspace": str(workspace),
        "worktree_name": worktree_name,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "usage": usage,
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    return CursorRunResult(
        command=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        output_ref=str(meta_path.resolve()),
        stdout_ref=str(stdout_path.resolve()),
        stderr_ref=str(stderr_path.resolve()),
        prompt_ref=str(prompt_path.resolve()),
        workspace=workspace,
        worktree_name=worktree_name,
        usage=usage,
        timed_out=timed_out,
        cancelled=cancelled,
    )


def run_cursor_smoke_test(
    *,
    workspace: Path,
    timeout_seconds: float = 120.0,
    agent_bin: str | Path | None = None,
    output_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> CursorSmokeResult:
    """Operator diagnostic: unattended headless invoke with JSON result parsing.

    Uses ``--print --output-format json`` (not text). Delivery workers keep text.
    """
    prompt = (
        "Do not modify any files. Do not run shell commands that change state. "
        "Return exactly: PROJECTOS_HEADLESS_OK"
    )
    run_id = f"cursor-smoke-{int(time.time())}"
    out_root = (
        Path(output_dir) if output_dir is not None else RUN_OUTPUT_DIR / run_id
    )
    cursor = invoke_cursor_agent(
        prompt=prompt,
        workspace=Path(workspace),
        run_id=run_id,
        timeout_seconds=timeout_seconds,
        agent_bin=agent_bin,
        output_dir=out_root,
        runner=runner,
        force=True,
        trust=True,
        mode=None,
        output_format="json",
    )
    return evaluate_cursor_smoke(cursor, evidence_dir=out_root)


SMOKE_TOKEN = "PROJECTOS_HEADLESS_OK"


@dataclass(frozen=True)
class CursorSmokeResult:
    cursor: CursorRunResult
    smoke_ok: bool
    reason: str
    parsed: dict[str, Any] | None
    result_text: str | None
    parsed_ref: str | None

    @property
    def exit_code(self) -> int:
        return 0 if self.smoke_ok else 1


def parse_cursor_print_json(stdout: str) -> dict[str, Any]:
    """Parse a single Cursor ``--output-format json`` completion object."""
    text = (stdout or "").strip()
    if not text:
        raise CursorAdapterError("Cursor JSON stdout was empty")
    # Prefer first complete JSON object (Cursor emits one object + newline).
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise CursorAdapterError("Cursor stdout is not valid JSON") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise CursorAdapterError(
                f"Cursor stdout is not valid JSON: {exc}"
            ) from exc
    if not isinstance(data, dict):
        raise CursorAdapterError("Cursor JSON root must be an object")
    return data


def extract_cursor_result_text(parsed: dict[str, Any]) -> str:
    """Return assistant/result text from a Cursor JSON envelope."""
    result = parsed.get("result")
    if isinstance(result, str):
        return result
    for key in ("message", "content", "text", "output"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def cursor_json_is_success(parsed: dict[str, Any]) -> bool:
    """True when Cursor reports a successful print-json completion."""
    if parsed.get("is_error") is True:
        return False
    subtype = parsed.get("subtype")
    if subtype is not None and str(subtype).lower() not in {"success", "ok"}:
        return False
    # Official envelope: type=result, subtype=success, is_error=false
    type_val = parsed.get("type")
    if type_val is not None and str(type_val).lower() not in {"result", "success"}:
        # Allow envelopes that omit type but carry a string result.
        if "result" not in parsed:
            return False
    return True


def evaluate_cursor_smoke(
    cursor: CursorRunResult,
    *,
    evidence_dir: Path | None = None,
) -> CursorSmokeResult:
    """Validate smoke success from process + parsed Cursor JSON result.

    returncode 0 alone is never sufficient.
    """
    parsed: dict[str, Any] | None = None
    result_text: str | None = None
    parsed_ref: str | None = None
    evidence = Path(evidence_dir) if evidence_dir is not None else None

    def _persist(payload: dict[str, Any]) -> str | None:
        if evidence is None:
            return None
        evidence.mkdir(parents=True, exist_ok=True)
        path = evidence / "parsed_result.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return str(path.resolve())

    if cursor.timed_out:
        parsed_ref = _persist(
            {"smoke_ok": False, "reason": "timed_out", "cursor_meta": {"timed_out": True}}
        )
        return CursorSmokeResult(
            cursor=cursor,
            smoke_ok=False,
            reason="timed_out",
            parsed=None,
            result_text=None,
            parsed_ref=parsed_ref,
        )
    if cursor.cancelled:
        parsed_ref = _persist(
            {
                "smoke_ok": False,
                "reason": "cancelled",
                "cursor_meta": {"cancelled": True},
            }
        )
        return CursorSmokeResult(
            cursor=cursor,
            smoke_ok=False,
            reason="cancelled",
            parsed=None,
            result_text=None,
            parsed_ref=parsed_ref,
        )
    if cursor.returncode != 0:
        parsed_ref = _persist(
            {
                "smoke_ok": False,
                "reason": f"nonzero_returncode:{cursor.returncode}",
                "returncode": cursor.returncode,
            }
        )
        return CursorSmokeResult(
            cursor=cursor,
            smoke_ok=False,
            reason=f"nonzero_returncode:{cursor.returncode}",
            parsed=None,
            result_text=None,
            parsed_ref=parsed_ref,
        )
    if not (cursor.stdout or "").strip():
        parsed_ref = _persist({"smoke_ok": False, "reason": "empty_stdout"})
        return CursorSmokeResult(
            cursor=cursor,
            smoke_ok=False,
            reason="empty_stdout",
            parsed=None,
            result_text=None,
            parsed_ref=parsed_ref,
        )

    try:
        parsed = parse_cursor_print_json(cursor.stdout)
    except CursorAdapterError as exc:
        parsed_ref = _persist(
            {
                "smoke_ok": False,
                "reason": "malformed_json",
                "error": str(exc),
                "stdout_excerpt": cursor.stdout[:500],
            }
        )
        return CursorSmokeResult(
            cursor=cursor,
            smoke_ok=False,
            reason="malformed_json",
            parsed=None,
            result_text=None,
            parsed_ref=parsed_ref,
        )

    if not cursor_json_is_success(parsed):
        parsed_ref = _persist(
            {
                "smoke_ok": False,
                "reason": "cursor_result_not_success",
                "parsed": parsed,
            }
        )
        return CursorSmokeResult(
            cursor=cursor,
            smoke_ok=False,
            reason="cursor_result_not_success",
            parsed=parsed,
            result_text=extract_cursor_result_text(parsed) or None,
            parsed_ref=parsed_ref,
        )

    result_text = extract_cursor_result_text(parsed).strip()
    if result_text != SMOKE_TOKEN:
        parsed_ref = _persist(
            {
                "smoke_ok": False,
                "reason": "wrong_result_text",
                "expected": SMOKE_TOKEN,
                "result_text": result_text,
                "parsed": parsed,
            }
        )
        return CursorSmokeResult(
            cursor=cursor,
            smoke_ok=False,
            reason="wrong_result_text",
            parsed=parsed,
            result_text=result_text,
            parsed_ref=parsed_ref,
        )

    parsed_ref = _persist(
        {
            "smoke_ok": True,
            "reason": "ok",
            "result_text": result_text,
            "parsed": parsed,
        }
    )
    return CursorSmokeResult(
        cursor=cursor,
        smoke_ok=True,
        reason="ok",
        parsed=parsed,
        result_text=result_text,
        parsed_ref=parsed_ref,
    )


def _run_via_callable(
    runner: Callable[..., Any],
    command: list[str],
    *,
    workspace: Path,
    timeout_seconds: float | None,
    cancel_event: threading.Event | None,
) -> tuple[str, str, int, bool, bool]:
    box: dict[str, Any] = {}
    done = threading.Event()

    def _target() -> None:
        try:
            box["completed"] = runner(
                command,
                cwd=str(workspace),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as exc:
            box["timeout"] = exc
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    deadline = None if timeout_seconds is None else (time.time() + float(timeout_seconds))
    while not done.wait(POLL_INTERVAL_SECONDS):
        if cancel_event is not None and cancel_event.is_set():
            return ("", "[projectos] cancelled", 130, False, True)
        if deadline is not None and time.time() >= deadline:
            return (
                "",
                f"[projectos] timed out after {timeout_seconds}s",
                124,
                True,
                False,
            )

    if "timeout" in box:
        exc = box["timeout"]
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        stderr = (stderr + f"\n[projectos] timed out after {timeout_seconds}s").strip()
        return stdout, stderr, 124, True, False
    if "error" in box:
        raise box["error"]
    completed = box["completed"]
    return (
        completed.stdout or "",
        completed.stderr or "",
        int(completed.returncode),
        False,
        False,
    )


def _pipe_reader(stream, chunks: list[str], done: threading.Event) -> None:
    try:
        while True:
            data = stream.read(4096)
            if not data:
                break
            chunks.append(data)
    except (OSError, ValueError):
        pass
    finally:
        done.set()
        try:
            stream.close()
        except OSError:
            pass


def _bounded_join(thread: threading.Thread, timeout: float) -> None:
    thread.join(timeout=max(0.01, timeout))


def _run_via_popen(
    command: list[str],
    *,
    workspace: Path,
    timeout_seconds: float | None,
    cancel_event: threading.Event | None,
) -> tuple[str, str, int, bool, bool]:
    """Poll-based execution with bounded timeout and process-tree kill."""
    creationflags = 0
    if sys.platform == "win32":
        # Isolate the spawned tree so Ctrl+C in the parent shell is less likely
        # to race with our own tree termination, and so we can target this PID.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=os.environ.copy(),
            creationflags=creationflags,
        )
    except FileNotFoundError as exc:
        raise CursorAdapterError(
            f"Failed to execute Cursor Agent: {exc}"
        ) from exc

    register_active_process(proc)
    timed_out = False
    cancelled = False
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    out_done = threading.Event()
    err_done = threading.Event()
    out_thread = threading.Thread(
        target=_pipe_reader,
        args=(proc.stdout, stdout_chunks, out_done),
        daemon=True,
    )
    err_thread = threading.Thread(
        target=_pipe_reader,
        args=(proc.stderr, stderr_chunks, err_done),
        daemon=True,
    )
    out_thread.start()
    err_thread.start()

    deadline = None if timeout_seconds is None else (time.time() + float(timeout_seconds))
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                if proc.pid:
                    terminate_process_tree(proc.pid)
                break
            if deadline is not None and time.time() >= deadline:
                timed_out = True
                if proc.pid:
                    terminate_process_tree(proc.pid)
                break
            time.sleep(POLL_INTERVAL_SECONDS)

        # Bounded drain of pipe reader threads after exit/kill.
        _bounded_join(out_thread, PIPE_DRAIN_SECONDS)
        _bounded_join(err_thread, PIPE_DRAIN_SECONDS)

        # Ensure process is reaped without indefinite wait.
        try:
            proc.wait(timeout=PIPE_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            if proc.pid:
                terminate_process_tree(proc.pid, grace_seconds=2.0)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if cancelled:
            stderr = (stderr + "\n[projectos] cancelled").strip()
            return stdout, stderr, 130, False, True
        if timed_out:
            stderr = (
                stderr + f"\n[projectos] timed out after {timeout_seconds}s"
            ).strip()
            return stdout, stderr, 124, True, False
        returncode = int(proc.returncode or 0)
        return stdout, stderr, returncode, False, False
    finally:
        unregister_active_process(proc)
