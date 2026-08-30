"""Governed delivery-project bootstrap from explicit Sponsor new-project intent."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from projectos.db import connection
from projectos.delivery.contract import infer_delivery_contract
from projectos.errors import OrchestrationError, ProjectctlError, RegistryConflictError
from projectos.gitutil import resolve_git_root
from projectos.migrate import initialize_database
from projectos.onboarding import register_project
from projectos.pm_agent import accept_sponsor_handoff
from projectos.project_defaults import load_project_defaults
from projectos.projectctl_bridge import (
    ProjectctlStatusResult,
    ensure_single_active_project,
    run_projectctl,
    run_projectctl_status,
)
from projectos.registry import load_registry_or_empty
from projectos.repository import REPOSITORY_TYPE_DELIVERY_PROJECT
from projectos.request_capability import classify_request
from projectos.services.context import ServiceContext
from projectos.slack_advisor_handoff import HandoffRequest
from projectos.slack_resolver import authorize_slack_channel, set_session_project
from projectos.slack_sponsor_format import SPONSOR_ACCEPTANCE
from projectos.slack_thread_context import mark_projectos_thread_active
from projectos.sponsor_execution_authority import (
    classify_sponsor_execution_authority,
    merge_authority_into_constraints,
)
from projectos.store import require_safe_id

_PRJ_NUM_RE = re.compile(r"^PRJ-(\d+)$", re.IGNORECASE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ProjectCreationRequest:
    sponsor_user_id: str
    team_id: str
    channel_id: str
    thread_ts: str
    source_message_ts: str
    objective: str
    raw_request: str
    event_id: str = ""
    dedup_key: str | None = None


@dataclass(frozen=True)
class ProjectCreationResult:
    project_human_id: str
    repository_root: Path
    handoff_id: str
    run_id: str
    reply_text: str
    idempotent_replay: bool = False


def _parse_prj_number(project_human_id: str) -> int | None:
    match = _PRJ_NUM_RE.match(str(project_human_id or "").strip().upper())
    if not match:
        return None
    return int(match.group(1))


def _known_project_numbers(registry_path: Path, conn: sqlite3.Connection) -> list[int]:
    numbers: list[int] = []
    registry = load_registry_or_empty(registry_path)
    for entry in registry.projects:
        parsed = _parse_prj_number(entry.project_human_id)
        if parsed is not None:
            numbers.append(parsed)
    rows = conn.execute("SELECT project_human_id FROM project_id_reservations").fetchall()
    for row in rows:
        parsed = _parse_prj_number(str(row["project_human_id"]))
        if parsed is not None:
            numbers.append(parsed)
    return numbers


def allocate_project_id(conn: sqlite3.Connection, *, registry_path: Path) -> str:
    """Concurrency-safe PRJ-### allocation (max registered/reserved + 1)."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        numbers = _known_project_numbers(registry_path, conn)
        next_num = (max(numbers) + 1) if numbers else 1
        while True:
            candidate = f"PRJ-{next_num:03d}"
            registry = load_registry_or_empty(registry_path)
            if registry.get(candidate) is None:
                try:
                    conn.execute(
                        "INSERT INTO project_id_reservations (project_human_id, source) VALUES (?, ?)",
                        (candidate, "allocator"),
                    )
                except sqlite3.IntegrityError:
                    next_num += 1
                    continue
                return candidate
            next_num += 1


def _release_project_id(conn: sqlite3.Connection, project_human_id: str) -> None:
    conn.execute(
        "DELETE FROM project_id_reservations WHERE project_human_id = ?",
        (project_human_id,),
    )


def derive_project_name(raw_request: str) -> str:
    text = str(raw_request or "").strip()
    lowered = text.casefold()
    patterns = (
        r"build a(?: simple)? (.+?)(?:\.|,| that | which )",
        r"project (?:to|for) (.+?)(?:\.|,)",
        r"called (.+?)(?:\.|,)",
    )
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            phrase = match.group(1).strip(" .")
            if phrase:
                return phrase[:80].title()
    return "New Delivery Project"


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", str(value or "").casefold()).strip("-")
    return slug[:48] or "project"


def _delivery_contract_for_request(*, product_name: str, repository_name: str) -> dict[str, Any]:
    contract = infer_delivery_contract(
        product_name=product_name,
        repository_owner="projectos",
        repository_name=repository_name,
        target_platforms=["any"],
        external_distribution=False,
    )
    contract.update(
        {
            "delivery_type": "python_cli",
            "packaging_adapter": "python_desktop",
            "installer_format": "zip",
            "installer_name_template": "{product}-Setup-{version}.zip",
            "github_release_enabled": False,
            "code_signing_policy": "not_required",
            "sbom_policy": "required",
        }
    )
    return contract


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "ProjectOS",
        "GIT_AUTHOR_EMAIL": "projectos@local",
        "GIT_COMMITTER_NAME": "ProjectOS",
        "GIT_COMMITTER_EMAIL": "projectos@local",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def _run_git(repo_root: Path, args: list[str]) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise OrchestrationError(f"git {' '.join(args)} failed: {detail}")


def _runtime_python() -> Path:
    return Path(sys.executable).resolve()


def _repository_venv_python(repo_root: Path) -> Path:
    if sys.platform == "win32":
        return (repo_root / ".venv" / "Scripts" / "python.exe").resolve()
    return (repo_root / ".venv" / "bin" / "python").resolve()


def _projectctl_db_path(repo_root: Path) -> Path:
    return (repo_root / "project-control" / "project.db").resolve()


def _parse_prj_numeric_id(project_human_id: str) -> int:
    match = _PRJ_NUM_RE.match(str(project_human_id or "").strip().upper())
    if not match:
        raise OrchestrationError(
            f"Project creation requires numeric PRJ-### id (got {project_human_id!r})"
        )
    return int(match.group(1))


def _create_repository_venv(repo_root: Path) -> Path:
    venv_dir = repo_root / ".venv"
    if venv_dir.exists():
        shutil.rmtree(venv_dir, ignore_errors=True)
    result = subprocess.run(
        [str(_runtime_python()), "-m", "venv", str(venv_dir)],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise OrchestrationError(f"venv creation failed: {detail}")
    python_executable = _repository_venv_python(repo_root)
    if not python_executable.is_file():
        raise OrchestrationError(
            f"venv creation did not produce interpreter at {python_executable}"
        )
    return python_executable


def _install_delivery_project_package(repo_root: Path, python_executable: Path) -> None:
    result = subprocess.run(
        [str(python_executable), "-m", "pip", "install", "-e", "."],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise OrchestrationError(f"delivery project package install failed: {detail}")


def _seed_inactive_projects_for_id(
    repo_root: Path,
    *,
    project_human_id: str,
    python_executable: Path,
) -> None:
    target_num = _parse_prj_numeric_id(project_human_id)
    db_path = _projectctl_db_path(repo_root)
    for index in range(1, target_num):
        run_projectctl(
            repo_root,
            [
                "project",
                "create",
                "--name",
                f"Bootstrap seed {index:03d}",
                "--inactive",
            ],
            python_executable=python_executable,
            db_path=db_path,
            require_zero=True,
        )


def validate_project_control_state(
    repo_root: Path,
    *,
    project_human_id: str,
    python_executable: Path,
) -> None:
    """Prove project-control is initialized and matches repository identity."""
    db_path = _projectctl_db_path(repo_root)
    marker = repo_root / "project-control" / ".projectctl-bootstrap"
    if marker.exists():
        raise OrchestrationError(
            "project-control contains synthetic bootstrap marker; refusing to register"
        )
    if not db_path.is_file() or db_path.stat().st_size == 0:
        raise OrchestrationError(
            f"project-control database missing or empty at {db_path}"
        )

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise OrchestrationError(
            f"project-control database is not readable SQLite at {db_path}: {exc}"
        ) from exc
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required = {"projects", "schema_migrations"}
        missing = sorted(required - tables)
        if missing:
            raise OrchestrationError(
                f"project-control schema incomplete; missing tables: {', '.join(missing)}"
            )
        active = conn.execute(
            "SELECT human_id FROM projects WHERE is_active = 1 ORDER BY id ASC"
        ).fetchone()
        if active is None:
            raise OrchestrationError(
                "project-control has no active project after initialization"
            )
        if str(active[0]) != project_human_id:
            raise OrchestrationError(
                "project-control active project "
                f"{active[0]!r} does not match repository identity {project_human_id!r}"
            )
    finally:
        conn.close()

    status = run_projectctl_status(repo_root, python_executable=python_executable)
    ensure_single_active_project(status, expected_human_id=project_human_id)


def _initialize_projectctl(
    repo_root: Path,
    *,
    project_human_id: str,
    project_name: str,
    python_executable: Path,
) -> None:
    db_path = _projectctl_db_path(repo_root)
    run_projectctl(
        repo_root,
        ["init"],
        python_executable=python_executable,
        db_path=db_path,
        require_zero=True,
    )
    _seed_inactive_projects_for_id(
        repo_root,
        project_human_id=project_human_id,
        python_executable=python_executable,
    )
    run_projectctl(
        repo_root,
        ["project", "create", "--name", project_name.strip()],
        python_executable=python_executable,
        db_path=db_path,
        require_zero=True,
    )
    validate_project_control_state(
        repo_root,
        project_human_id=project_human_id,
        python_executable=python_executable,
    )


def bootstrap_project_repository(
    *,
    projects_root: Path,
    template_root: Path,
    project_human_id: str,
    project_name: str,
    raw_request: str,
) -> Path:
    """Create and initialize a delivery repository under projects_root."""
    slug = _slugify(project_name)
    repo_dir = (projects_root / f"{slug}-{project_human_id}").resolve()
    if repo_dir.exists():
        raise OrchestrationError(f"project repository path already exists: {repo_dir}")

    staging = (projects_root / f".staging-{project_human_id}").resolve()
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        if template_root.is_dir():
            for item in template_root.iterdir():
                if item.name.startswith(".") and item.name != ".gitignore":
                    continue
                dest = staging / item.name
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
        else:
            staging.mkdir(parents=True, exist_ok=True)

        project_dir = staging / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        identity = {
            "schema_version": 1,
            "repository_type": REPOSITORY_TYPE_DELIVERY_PROJECT,
            "project_human_id": project_human_id,
            "project_name": project_name,
            "isolation_model": "one-project-per-repository",
            "orchestration_scope": "project",
            "cross_project_access": False,
        }
        (project_dir / "repository.json").write_text(
            json.dumps(identity, indent=2) + "\n",
            encoding="utf-8",
        )
        delivery = _delivery_contract_for_request(
            product_name=project_name,
            repository_name=slug,
        )
        (project_dir / "delivery.json").write_text(
            json.dumps(delivery, indent=2) + "\n",
            encoding="utf-8",
        )
        (staging / "OBJECTIVE.md").write_text(
            f"# Sponsor objective\n\n{raw_request.strip()}\n",
            encoding="utf-8",
        )

        _run_git(staging, ["init", "-b", "main"])
        _run_git(staging, ["add", "-A"])
        _run_git(staging, ["commit", "-m", "chore: bootstrap delivery project"])

        resolve_git_root(staging)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(repo_dir)

        python_executable = _create_repository_venv(repo_dir)
        _install_delivery_project_package(repo_dir, python_executable)
        try:
            _initialize_projectctl(
                repo_dir,
                project_human_id=project_human_id,
                project_name=project_name,
                python_executable=python_executable,
            )
        except (ProjectctlError, OrchestrationError) as exc:
            raise OrchestrationError(
                f"Project creation failed during projectctl initialization: {exc}"
            ) from exc
        _run_git(repo_dir, ["add", "-A"])
        commit = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        if commit.returncode != 0:
            _run_git(repo_dir, ["commit", "-m", "chore: initialize project-control"])
        return repo_dir
    except Exception:
        _remove_repository_tree(staging)
        _remove_repository_tree(repo_dir)
        raise


def _lookup_creation_record(conn: sqlite3.Connection, dedup_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT project_human_id, handoff_id, run_id, repository_root, objective
        FROM slack_project_creations
        WHERE dedup_key = ?
        """,
        (dedup_key,),
    ).fetchone()
    if row is None:
        return None
    return {
        "project_human_id": row["project_human_id"],
        "handoff_id": row["handoff_id"],
        "run_id": row["run_id"],
        "repository_root": row["repository_root"],
        "objective": row["objective"],
    }


def _record_creation(
    conn: sqlite3.Connection,
    *,
    dedup_key: str,
    request: ProjectCreationRequest,
    result: ProjectCreationResult,
) -> None:
    conn.execute(
        """
        INSERT INTO slack_project_creations (
            dedup_key, team_id, channel_id, message_ts, event_id,
            project_human_id, handoff_id, run_id, repository_root, objective
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dedup_key,
            request.team_id,
            request.channel_id,
            request.source_message_ts,
            request.event_id,
            result.project_human_id,
            result.handoff_id,
            result.run_id,
            str(result.repository_root),
            request.raw_request,
        ),
    )


def _build_handoff(
    project_id: str,
    raw_request: str,
    *,
    sponsor_user_id: str,
) -> HandoffRequest:
    cap = classify_request(text=raw_request, fallback_objective=raw_request)
    desired = dict(cap.desired_outputs)
    lowered = raw_request.casefold()
    if "zip" in lowered:
        desired["zip_package"] = True
    if "test" in lowered:
        desired["automated_tests"] = True
    constraints = dict(cap.constraints)
    constraints["preserve_raw_sponsor_request"] = True
    authority = classify_sponsor_execution_authority(
        raw_request,
        explicit_new_project=True,
        authenticated_sponsor_action=True,
        authority_ingress="slack_new_project",
        sponsor_user_id=sponsor_user_id,
    )
    constraints_json = merge_authority_into_constraints(
        json.dumps(constraints, sort_keys=True),
        authority,
    )
    return HandoffRequest(
        project_id=project_id,
        objective=raw_request.strip(),
        action_type="work_request",
        rationale="Sponsor explicitly requested a new governed delivery project.",
        scope="",
        constraints=constraints_json,
        acceptance_intent=SPONSOR_ACCEPTANCE,
        exclusions="",
        source_conversation_summary=raw_request.strip()[:1500],
        desired_outputs_json=json.dumps(desired, sort_keys=True),
    )


def _format_creation_reply(
    *,
    project_human_id: str,
    project_name: str,
    objective: str,
    run_id: str,
    idempotent: bool = False,
    execution_note: str = "",
) -> str:
    prefix = "New project already initiated" if idempotent else "New project initiated"
    objective_line = objective.strip().splitlines()[0][:240]
    lines = [
        f"{prefix}: `{project_human_id}`",
        f"Objective: {objective_line}",
        f"Run: `{run_id}`",
        f"PM planning/execution started for {project_name}.",
    ]
    note = str(execution_note or "").strip()
    if note and "Sponsor-authorized work" not in note:
        lines.append(note)
    return "\n".join(lines)


def _remove_repository_tree(path: Path | None) -> None:
    if path is None:
        return
    target = Path(path)
    if not target.exists():
        return

    def _on_rm_error(func, p, exc_info):
        import stat

        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        func(p)

    shutil.rmtree(target, onerror=_on_rm_error)
    if target.exists():
        import time

        for _ in range(10):
            shutil.rmtree(target, onerror=_on_rm_error)
            if not target.exists():
                break
            time.sleep(0.1)


def create_project_from_sponsor_request(
    ctx: ServiceContext,
    request: ProjectCreationRequest,
    *,
    projectctl_runner: Callable[..., ProjectctlStatusResult] | None = None,
    defaults_path: Path | str | None = None,
) -> ProjectCreationResult:
    """Bootstrap repository, register, bind Slack context, and start PM orchestration."""
    initialize_database(ctx.db_path)
    dedup_key = str(request.dedup_key or "").strip()
    if not dedup_key:
        dedup_key = (
            f"message:{request.team_id}:{request.channel_id}:{request.source_message_ts}"
        )

    with connection(ctx.db_path) as conn:
        if not authorize_slack_channel(
            conn, channel_id=request.channel_id, team_id=request.team_id
        ):
            raise OrchestrationError("This Slack channel is not authorized for ProjectOS.")

        existing = _lookup_creation_record(conn, dedup_key)
        if existing:
            return ProjectCreationResult(
                project_human_id=existing["project_human_id"],
                repository_root=Path(existing["repository_root"]),
                handoff_id=str(existing["handoff_id"] or ""),
                run_id=existing["run_id"],
                reply_text=_format_creation_reply(
                    project_human_id=existing["project_human_id"],
                    project_name=derive_project_name(existing["objective"]),
                    objective=existing["objective"],
                    run_id=existing["run_id"],
                    idempotent=True,
                ),
                idempotent_replay=True,
            )

        project_human_id = allocate_project_id(conn, registry_path=ctx.registry_path)
        project_name = derive_project_name(request.raw_request)
        defaults = load_project_defaults(defaults_path)
        defaults.projects_root.mkdir(parents=True, exist_ok=True)
        conn.commit()

    repository_root: Path | None = None
    try:
        repository_root = bootstrap_project_repository(
            projects_root=defaults.projects_root,
            template_root=defaults.delivery_template_root,
            project_human_id=project_human_id,
            project_name=project_name,
            raw_request=request.raw_request,
        )
        register_project(
            repository_root,
            registry_path=ctx.registry_path,
            projectctl_runner=projectctl_runner,
        )
    except Exception as exc:
        with connection(ctx.db_path) as conn:
            _release_project_id(conn, project_human_id)
            conn.commit()
        _remove_repository_tree(repository_root)
        phase = "bootstrap"
        message = str(exc)
        lowered = message.casefold()
        if "projectctl initialization" in lowered:
            phase = "projectctl initialization"
        elif isinstance(exc, RegistryConflictError):
            phase = "registry"
        raise OrchestrationError(f"Project creation failed during {phase}: {exc}") from exc

    require_safe_id(request.sponsor_user_id, label="sponsor_user_id")
    with connection(ctx.db_path) as conn:
        set_session_project(
            conn,
            team_id=request.team_id,
            channel_id=request.channel_id,
            thread_ts=request.thread_ts,
            user_id=request.sponsor_user_id,
            project_human_id=project_human_id,
        )
        mark_projectos_thread_active(
            conn,
            team_id=request.team_id,
            channel_id=request.channel_id,
            thread_ts=request.thread_ts,
        )

        handoff = _build_handoff(
            project_human_id,
            request.raw_request,
            sponsor_user_id=request.sponsor_user_id,
        )
        pm_result = accept_sponsor_handoff(
            ctx,
            conn,
            handoff=handoff,
            project_id=project_human_id,
            team_id=request.team_id,
            channel_id=request.channel_id,
            thread_ts=request.thread_ts,
            sponsor_user_id=request.sponsor_user_id,
            advisor_text="",
            request_type_override="WORK",
            explicit_new_project=True,
        )

        result = ProjectCreationResult(
            project_human_id=project_human_id,
            repository_root=repository_root,
            handoff_id=pm_result.handoff_id,
            run_id=pm_result.run_id,
            reply_text=_format_creation_reply(
                project_human_id=project_human_id,
                project_name=project_name,
                objective=request.raw_request,
                run_id=pm_result.run_id,
                execution_note=pm_result.execution_evidence or "",
            ),
        )
        _record_creation(conn, dedup_key=dedup_key, request=request, result=result)
        conn.commit()
        return result


def handle_new_project_sponsor_request(
    ctx: ServiceContext,
    *,
    raw_text: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    sponsor_user_id: str,
    source_message_ts: str,
    event_id: str = "",
    dedup_key: str | None = None,
    projectctl_runner: Callable[..., ProjectctlStatusResult] | None = None,
    defaults_path: Path | str | None = None,
) -> dict[str, Any]:
    """Slack-facing entry for explicit new-project Sponsor requests."""
    request = ProjectCreationRequest(
        sponsor_user_id=sponsor_user_id,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        source_message_ts=source_message_ts,
        objective=raw_text.strip(),
        raw_request=raw_text.strip(),
        event_id=event_id,
        dedup_key=dedup_key,
    )
    try:
        result = create_project_from_sponsor_request(
            ctx,
            request,
            projectctl_runner=projectctl_runner,
            defaults_path=defaults_path,
        )
    except OrchestrationError as exc:
        return {"text": str(exc), "response_type": "ephemeral"}
    return {"text": result.reply_text, "response_type": "in_channel"}
