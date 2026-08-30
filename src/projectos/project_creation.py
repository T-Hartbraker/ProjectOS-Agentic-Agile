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
from projectos.projectctl_bridge import ProjectctlStatusResult, run_projectctl
from projectos.registry import load_registry_or_empty
from projectos.repository import REPOSITORY_TYPE_DELIVERY_PROJECT
from projectos.request_capability import classify_request
from projectos.services.context import ServiceContext
from projectos.slack_advisor_handoff import HandoffRequest
from projectos.slack_resolver import authorize_slack_channel, set_session_project
from projectos.slack_sponsor_format import SPONSOR_ACCEPTANCE
from projectos.slack_thread_context import mark_projectos_thread_active
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


def _ensure_repo_venv(repo_root: Path) -> Path:
    if sys.platform == "win32":
        target = repo_root / ".venv" / "Scripts" / "python.exe"
    else:
        target = repo_root / ".venv" / "bin" / "python"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        shutil.copy(sys.executable, target)
    return target.resolve()


def _initialize_projectctl(
    repo_root: Path,
    *,
    project_human_id: str,
    project_name: str,
    python_executable: Path,
) -> None:
    try:
        run_projectctl(
            repo_root,
            [
                "project",
                "init",
                "--human-id",
                project_human_id,
                "--name",
                project_name,
            ],
            python_executable=python_executable,
            require_zero=True,
        )
        return
    except ProjectctlError:
        control = repo_root / "project-control"
        control.mkdir(parents=True, exist_ok=True)
        db_path = control / "project.db"
        if not db_path.is_file():
            db_path.write_bytes(b"")
        marker = control / ".projectctl-bootstrap"
        marker.write_text(
            json.dumps(
                {
                    "project_human_id": project_human_id,
                    "project_name": project_name,
                    "bootstrap": "projectos",
                }
            ),
            encoding="utf-8",
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
                if item.name.startswith("."):
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

        python_executable = _ensure_repo_venv(staging)
        _initialize_projectctl(
            staging,
            project_human_id=project_human_id,
            project_name=project_name,
            python_executable=python_executable,
        )
        _run_git(staging, ["add", "-A"])
        commit = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=staging,
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        if commit.returncode != 0:
            _run_git(staging, ["commit", "-m", "chore: initialize project-control"])

        resolve_git_root(staging)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(repo_dir)
        return repo_dir
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if repo_dir.exists():
            shutil.rmtree(repo_dir, ignore_errors=True)
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


def _build_handoff(project_id: str, raw_request: str) -> HandoffRequest:
    cap = classify_request(text=raw_request, fallback_objective=raw_request)
    desired = dict(cap.desired_outputs)
    lowered = raw_request.casefold()
    if "zip" in lowered:
        desired["zip_package"] = True
    if "test" in lowered:
        desired["automated_tests"] = True
    constraints = dict(cap.constraints)
    constraints["preserve_raw_sponsor_request"] = True
    return HandoffRequest(
        project_id=project_id,
        objective=raw_request.strip(),
        action_type="work_request",
        rationale="Sponsor explicitly requested a new governed delivery project.",
        scope="",
        constraints=json.dumps(constraints, sort_keys=True),
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
) -> str:
    prefix = "New project already initiated" if idempotent else "New project initiated"
    objective_line = objective.strip().splitlines()[0][:240]
    return (
        f"{prefix}: `{project_human_id}`\n"
        f"Objective: {objective_line}\n"
        f"Run: `{run_id}`\n"
        f"PM planning/execution started for {project_name}."
    )


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
            _release_project_id(conn, project_human_id)
            phase = "bootstrap"
            if isinstance(exc, RegistryConflictError):
                phase = "registry"
            raise OrchestrationError(f"Project creation failed during {phase}: {exc}") from exc

        require_safe_id(request.sponsor_user_id, label="sponsor_user_id")
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

        handoff = _build_handoff(project_human_id, request.raw_request)
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
