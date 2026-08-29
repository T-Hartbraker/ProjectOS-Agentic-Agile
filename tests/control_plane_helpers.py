"""Shared helpers for control-plane closure integration tests."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.delivery.contract import infer_delivery_contract
from projectos.domain_events import ACTOR_PM, EventContext
from projectos.execution_run import create_execution_run, get_execution_run, update_execution_run
from projectos.migrate import initialize_database
from projectos.qa_handoff import create_assurance_jobs_for_delivery, record_assurance_result
from projectos.qa_manager import execute_qa_manager_aggregation
from projectos.qa_gate import collect_qa_gate_facts, emit_qa_gate_evaluation
from projectos.services.context import ServiceContext
from projectos.sponsor_handoff import create_sponsor_handoff, mark_handoff_accepted
from projectos.store import create_job, get_job, mark_succeeded


def git_head(repo_root: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True
    ).strip()


def git_commit_file(repo_root: Path, relative_path: str, content: str) -> str:
    path = repo_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", relative_path], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            f"test: {relative_path}",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return git_head(repo_root)


def delivery_json(**overrides: Any) -> dict[str, Any]:
    base = infer_delivery_contract(
        product_name="ExampleProduct",
        repository_owner="acme",
        repository_name="example-product",
        target_platforms=["windows-x64"],
        external_distribution=True,
    )
    base.update({"installer_format": "zip", "sbom_policy": "required"})
    base.update(overrides)
    return base


def setup_release_project(
    tmp_path: Path,
    *,
    project_id: str = "PRJ-004",
    github_release_enabled: bool = True,
) -> tuple[ServiceContext, Path, str]:
    repo = init_git_repo(tmp_path / "product")
    write_identity(repo, project_human_id=project_id, project_name="Example Product")
    (repo / "pyproject.toml").write_text(
        "[project]\nname='example'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (repo / "project").mkdir(exist_ok=True)
    (repo / "project" / "delivery.json").write_text(
        json.dumps(
            delivery_json(
                github_release_enabled=github_release_enabled,
                code_signing_policy="not_required",
            )
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "project setup",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    candidate_a = git_commit_file(repo, "src/feature.txt", "feature A\n")
    venv_python = repo / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(sys.executable, venv_python)
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": project_id, "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    ctx = ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")
    return ctx, repo, candidate_a


def create_release_handoff(
    conn,
    *,
    project_id: str,
    candidate_objective: str = "publish release",
) -> tuple[Any, Any, EventContext]:
    desired = {
        "package": True,
        "publish": True,
        "return_download_link": True,
    }
    handoff = create_sponsor_handoff(
        conn,
        project_id=project_id,
        team_id="T1",
        channel_id="C1",
        thread_ts="1.0",
        sponsor_user_id="U1",
        request_type="RELEASE",
        objective=candidate_objective,
        desired_outputs_json=json.dumps(desired),
    )
    run = create_execution_run(
        conn,
        project_id=project_id,
        handoff_id=handoff.handoff_id,
        request_type="RELEASE",
        objective=candidate_objective,
    )
    mark_handoff_accepted(conn, handoff_id=handoff.handoff_id, run_id=run.run_id)
    update_execution_run(conn, run_id=run.run_id, status="RUNNING")
    event_ctx = EventContext(
        project_id=project_id,
        handoff_id=handoff.handoff_id,
        run_id=run.run_id,
    )
    return handoff, run, event_ctx


def git_parent(repo_root: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD^"], cwd=repo_root, text=True
    ).strip()


def run_full_qa_for_candidate(
    conn,
    *,
    repo_root: str,
    project_id: str,
    run_id: str,
    candidate_sha: str,
    delivery_human_id: str | None = None,
) -> str:
    base_sha = git_parent(Path(repo_root))
    hid = delivery_human_id or f"DEL-{run_id[-8:]}"
    delivery = create_job(
        conn,
        human_id=hid,
        project_human_id=project_id,
        repository_root=repo_root,
        agent_role="DELIVERY",
        queue="DELIVERY",
        status="SUCCEEDED",
        base_git_sha=base_sha,
        run_id=run_id,
    )
    conn.execute(
        "UPDATE orchestration_jobs SET candidate_git_sha = ? WHERE id = ?",
        (candidate_sha, delivery.id),
    )
    delivery = get_job(conn, delivery.id)
    handoff = create_assurance_jobs_for_delivery(conn, delivery, candidate_git_sha=candidate_sha)
    for hid in handoff.assurance_job_ids:
        if "QA_MANAGER" in hid:
            continue
        row = conn.execute(
            "SELECT id FROM orchestration_jobs WHERE human_id = ?", (hid,)
        ).fetchone()
        assurance = get_job(conn, int(row["id"]))
        record_assurance_result(conn, assurance, verdict="PASS", evidence_ref="ok")
        conn.execute(
            "UPDATE qa_evidence SET run_id = ? WHERE assurance_job_id = ?",
            (run_id, assurance.id),
        )
    mgr_row = conn.execute(
        "SELECT id FROM orchestration_jobs WHERE human_id = ?",
        (f"{delivery.human_id}__QA_MANAGER",),
    ).fetchone()
    mgr_job = get_job(conn, int(mgr_row["id"]))
    mark_succeeded(conn, mgr_job.id, output_ref="mgr", candidate_git_sha=candidate_sha)
    execute_qa_manager_aggregation(conn, mgr_job)
    conn.execute(
        "UPDATE qa_evidence SET run_id = ? WHERE assurance_job_id = ?",
        (run_id, mgr_job.id),
    )
    facts = collect_qa_gate_facts(
        conn,
        project_id=project_id,
        candidate_git_sha=candidate_sha,
        run_id=run_id,
    )
    assert str(facts.get("gate")) == "PASSED"
    emit_qa_gate_evaluation(
        conn,
        project_id=project_id,
        event_context=EventContext(project_id=project_id, run_id=run_id),
        candidate_git_sha=candidate_sha,
        run_id=run_id,
    )
    return hid


def assert_run_not_completed(conn, run_id: str) -> None:
    run = get_execution_run(conn, run_id)
    assert run is not None
    assert run.status != "COMPLETED"


def count_events(conn, *, run_id: str, event_type: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM projectos_events
        WHERE run_id = ? AND event_type = ?
        """,
        (run_id, event_type),
    ).fetchone()
    return int(row["c"])
