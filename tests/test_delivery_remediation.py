"""Recoverable delivery remediation tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.domain_events import EventContext
from projectos.migrate import initialize_database
from projectos.pm_delivery_remediation import (
    attempt_delivery_contract_remediation,
    classify_failure_recoverability,
    handle_capability_gap,
)
from projectos.delivery.contract import delivery_contract_missing_evidence, delivery_json_path
from projectos.execution_run import create_execution_run, update_execution_run
from projectos.services.context import ServiceContext
from projectos.sponsor_handoff import create_sponsor_handoff


def _ctx(tmp_path: Path, *, with_delivery: bool = False) -> tuple[ServiceContext, Path]:
    repo = init_git_repo(tmp_path / "product")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    if with_delivery:
        (repo / "project").mkdir(exist_ok=True)
        (repo / "project" / "delivery.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "delivery_type": "desktop_application",
                    "target_platforms": ["windows-x64"],
                    "packaging_adapter": "generic",
                    "repository_provider": "github",
                    "repository_owner": "acme",
                    "repository_name": "gamma",
                    "default_branch": "main",
                    "release_strategy": "semantic_version",
                    "installer_format": "exe",
                    "installer_name_template": "{product}-Setup-{version}.exe",
                    "artifact_retention": 10,
                    "code_signing_policy": "not_required",
                    "sbom_policy": "required",
                    "checksum_policy": "sha256",
                    "github_release_enabled": False,
                    "slack_release_announcement_enabled": False,
                }
            ),
            encoding="utf-8",
        )
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json"), repo


def _seed_run(conn) -> EventContext:
    handoff = create_sponsor_handoff(
        conn,
        project_id="PRJ-003",
        team_id="T1",
        channel_id="C1",
        thread_ts="1.0",
        sponsor_user_id="U1",
        request_type="RELEASE",
        objective="ship",
    )
    run = create_execution_run(
        conn,
        project_id="PRJ-003",
        handoff_id=handoff.handoff_id,
        request_type="RELEASE",
        objective="ship",
    )
    update_execution_run(conn, run_id=run.run_id, status="RUNNING")
    return EventContext(project_id="PRJ-003", handoff_id=handoff.handoff_id, run_id=run.run_id)


def test_delivery_contract_missing_is_recoverable_configuration(tmp_path: Path) -> None:
    ctx, repo = _ctx(tmp_path)
    failure = delivery_contract_missing_evidence(repo)
    assert classify_failure_recoverability(failure) == "RECOVERABLE_CONFIGURATION"


def test_missing_delivery_contract_auto_remediates_and_writes_file(tmp_path: Path) -> None:
    ctx, repo = _ctx(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/gamma.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    failure = delivery_contract_missing_evidence(repo)
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        result = attempt_delivery_contract_remediation(
            conn,
            event_ctx=event_ctx,
            repo_root=repo,
            failure=failure,
        )
        events = {
            row["event_type"]
            for row in conn.execute(
                "SELECT event_type FROM projectos_events WHERE run_id = ?",
                (event_ctx.run_id,),
            ).fetchall()
        }
    assert result.recovered
    assert delivery_json_path(repo).is_file()
    assert "DELIVERY_CONTRACT_MISSING" in events
    assert "PM_REPLAN" in events


def test_capability_gap_installer_requires_sponsor(tmp_path: Path) -> None:
    ctx, repo = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        result = handle_capability_gap(
            conn,
            event_ctx=event_ctx,
            gap={
                "blocker_type": "INSTALLER_BACKEND_MISSING",
                "reason": "installer backend absent",
                "retryable": True,
            },
            project_id="PRJ-003",
            repository_root=str(repo.resolve()),
        )
        run = conn.execute(
            "SELECT status FROM execution_runs WHERE run_id = ?", (event_ctx.run_id,)
        ).fetchone()
        blocked = conn.execute(
            "SELECT 1 FROM projectos_events WHERE run_id = ? AND event_type = 'RUN_BLOCKED'",
            (event_ctx.run_id,),
        ).fetchone()
    assert result.recoverability == "SPONSOR_DECISION_REQUIRED"
    assert run["status"] == "WAITING_FOR_SPONSOR"
    assert blocked is None
