"""Closed-loop orchestration regression tests (FAT #2 successor)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.domain_events import EventContext, emit_projectos_event
from projectos.execution_run import create_execution_run, update_execution_run
from projectos.migrate import initialize_database
from projectos.pm_agent import accept_sponsor_handoff, orchestrate_release_capability
from projectos.pm_remediation import run_qa_with_remediation
from projectos.run_evidence import close_execution_run, pause_run_for_sponsor_decision
from projectos.run_outcomes import OUTCOME_SUCCESS, OUTCOME_UNRECOVERABLE_TECHNICAL
from projectos.run_state import apply_event_to_run
from projectos.services.context import ServiceContext
from projectos.slack_advisor_handoff import HandoffRequest
from projectos.slack_activity_blocks import activity_event_to_blocks
from projectos.sponsor_handoff import create_sponsor_handoff, mark_handoff_accepted
from projectos.sponsor_query import SponsorQueryService
from projectos.qa_semantics import collect_assurance_facts
from projectos.store import add_slack_interface_channel

TEAM = "T1"
CHANNEL = "C0BSYCCDRST"
THREAD = "1788023487.700189"


def _ctx(tmp_path: Path, *, with_delivery_json: bool = True) -> ServiceContext:
    repo = init_git_repo(tmp_path / "product")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    if with_delivery_json:
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
                    "trusted_build_command": "echo build",
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
    with connection(db) as conn:
        add_slack_interface_channel(conn, channel_id=CHANNEL, team_id=TEAM, is_default=True)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def _seed_qa_evidence(conn, *, total: int = 16, failed: int = 8) -> None:
    for i in range(total):
        result = "fail" if i < failed else "pass"
        conn.execute(
            """
            INSERT INTO qa_evidence (
                project_human_id, repository_root, candidate_git_sha,
                assurance_role, result, created_at
            ) VALUES ('PRJ-003', '/repo', ?, ?, ?, datetime('now'))
            """,
            (f"sha{i:04d}", f"ASSURANCE_{i % 4}", result),
        )


def _seed_handoff_run(conn, *, run_id: str | None = None) -> EventContext:
    handoff = create_sponsor_handoff(
        conn,
        project_id="PRJ-003",
        team_id=TEAM,
        channel_id=CHANNEL,
        thread_ts=THREAD,
        sponsor_user_id="U1",
        request_type="RELEASE",
        objective="re-release package and installer",
    )
    run = create_execution_run(
        conn,
        project_id="PRJ-003",
        handoff_id=handoff.handoff_id,
        request_type="RELEASE",
        objective=handoff.objective,
    )
    actual_run_id = run.run_id
    if run_id and run_id != actual_run_id:
        conn.execute(
            "UPDATE execution_runs SET run_id = ? WHERE run_id = ?",
            (run_id, actual_run_id),
        )
        actual_run_id = run_id
    mark_handoff_accepted(conn, handoff_id=handoff.handoff_id, run_id=actual_run_id)
    update_execution_run(conn, run_id=actual_run_id, status="RUNNING")
    return EventContext(
        project_id="PRJ-003",
        handoff_id=handoff.handoff_id,
        run_id=actual_run_id,
        slack_channel_id=CHANNEL,
        slack_thread_ts=THREAD,
    )


def _release_handoff() -> HandoffRequest:
    return HandoffRequest(
        project_id="PRJ-003",
        objective="re-release package and installer",
        action_type="prepare_release",
        rationale="",
        scope="",
        constraints="",
        acceptance_intent="",
        exclusions="",
        source_conversation_summary="",
    )


def test_qa_fail_remediation_pass_no_run_blocked(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        _seed_qa_evidence(conn, total=16, failed=8)
        event_ctx = _seed_handoff_run(conn)
    evidence = orchestrate_release_capability(
        ctx, event_ctx=event_ctx, project_id="PRJ-003", handoff=_release_handoff()
    )
    with connection(ctx.db_path) as conn:
        types = {
            row["event_type"]
            for row in conn.execute(
                "SELECT event_type FROM projectos_events WHERE run_id = ?", (event_ctx.run_id,)
            ).fetchall()
        }
        run = conn.execute(
            "SELECT status FROM execution_runs WHERE run_id = ?", (event_ctx.run_id,)
        ).fetchone()
    assert "REMEDIATION_STARTED" in types or "QA_GATE_PASSED" in types
    assert "RUN_BLOCKED" not in types
    assert run["status"] == "RUNNING"
    assert "remediation" in evidence.lower() or "QA gate" in evidence


def test_qa_failure_alone_never_terminalizes(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        _seed_qa_evidence(conn, total=4, failed=4)
        event_ctx = _seed_handoff_run(conn, run_id="RUN-NB")
        run_qa_with_remediation(conn, event_ctx=event_ctx, project_id="PRJ-003", max_cycles=0)
        blocked = conn.execute(
            "SELECT 1 FROM projectos_events WHERE run_id = ? AND event_type = 'RUN_BLOCKED'",
            (event_ctx.run_id,),
        ).fetchone()
    assert blocked is None


def test_terminal_run_not_reopened_by_operational_events(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_handoff_run(conn, run_id="RUN-LOCK")
        close_execution_run(
            conn,
            event_ctx=event_ctx,
            outcome=OUTCOME_UNRECOVERABLE_TECHNICAL,
            summary="PM blocked run.",
        )
        apply_event_to_run(
            conn,
            run_id="RUN-LOCK",
            event_type="QA_GATE_FAILED",
            payload={"phase": "QA_GATE", "actor_id": "qa-agent", "progress": 40},
        )
        apply_event_to_run(
            conn,
            run_id="RUN-LOCK",
            event_type="PHASE_CHANGED",
            payload={"phase": "DELIVERY", "actor_id": "pm-agent", "progress": 25},
        )
        run = conn.execute(
            "SELECT status, current_phase FROM execution_runs WHERE run_id = 'RUN-LOCK'"
        ).fetchone()
    assert run["status"] == "BLOCKED"


def test_waiting_for_sponsor_pauses_without_terminalizing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_handoff_run(conn, run_id="RUN-WAIT")
        pause_run_for_sponsor_decision(
            conn, event_ctx=event_ctx, summary="Need signing policy decision."
        )
        run = conn.execute(
            "SELECT status FROM execution_runs WHERE run_id = 'RUN-WAIT'"
        ).fetchone()
        terminal = conn.execute(
            "SELECT 1 FROM projectos_events WHERE run_id = 'RUN-WAIT' AND event_type = 'RUN_BLOCKED'"
        ).fetchone()
    assert run["status"] == "WAITING_FOR_SPONSOR"
    assert terminal is None


def test_recoverable_delivery_contract_missing_remediates(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, with_delivery_json=False)
    repo_root = json.loads(
        (tmp_path / "projects.json").read_text(encoding="utf-8")
    )["projects"][0]["repository_root"]
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/gamma.git"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    with connection(ctx.db_path) as conn:
        _seed_qa_evidence(conn, total=16, failed=0)
        event_ctx = _seed_handoff_run(conn, run_id="RUN-DELIV")
    orchestrate_release_capability(
        ctx, event_ctx=event_ctx, project_id="PRJ-003", handoff=_release_handoff()
    )
    with connection(ctx.db_path) as conn:
        types = {
            row["event_type"]
            for row in conn.execute(
                "SELECT event_type FROM projectos_events WHERE run_id = 'RUN-DELIV'"
            ).fetchall()
        }
        run = conn.execute(
            "SELECT status FROM execution_runs WHERE run_id = 'RUN-DELIV'"
        ).fetchone()
    assert "DELIVERY_CONTRACT_MISSING" in types or "PM_REPLAN" in types
    assert "RUN_BLOCKED" not in types
    assert run["status"] == "RUNNING"
    assert Path(repo_root, "project", "delivery.json").is_file()


def test_handoff_accepted_outbox_includes_request_type(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        emit_projectos_event(
            conn,
            ctx=EventContext(
                project_id="PRJ-003",
                handoff_id="HND-1",
                run_id="RUN-1",
                slack_channel_id="C1",
                slack_thread_ts="1.0",
            ),
            event_type="HANDOFF_ACCEPTED",
            summary="release package",
            metadata={"request_type": "RELEASE"},
        )
        row = conn.execute(
            "SELECT payload_json FROM event_outbox WHERE subscriber = 'slack' LIMIT 1"
        ).fetchone()
    payload = json.loads(row["payload_json"])
    blocks = activity_event_to_blocks(payload)
    assert payload.get("request_type") == "RELEASE"
    assert "RELEASE" in json.dumps(blocks)


def test_blocker_question_uses_active_run_evidence(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, with_delivery_json=False)
    with connection(ctx.db_path) as conn:
        _seed_qa_evidence(conn, total=16, failed=8)
        event_ctx = _seed_handoff_run(conn, run_id="RUN-BLK")
        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="QA_GATE_FAILED",
            summary="QA gate failed.",
            phase="QA_GATE",
            evidence={"gate": "FAILED"},
        )
        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="REMEDIATION_REQUIRED",
            summary="PM requires remediation.",
            phase="REMEDIATION",
        )
    answer = SponsorQueryService(ctx).get_blocker_summary("PRJ-003")
    assert "qa" in answer.lower() or "remediation" in answer.lower() or "delivery" in answer.lower()


def test_qa_semantics_preserve_distinct_job_and_review_counts(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        _seed_qa_evidence(conn, total=16, failed=0)
        for i in range(20):
            conn.execute(
                """
                INSERT INTO orchestration_jobs (
                    human_id, project_human_id, repository_root, queue, status,
                    agent_role, created_at, updated_at
                ) VALUES (?, 'PRJ-003', '/repo', 'ASSURANCE_FUNCTIONAL', 'SUCCEEDED',
                          'ASSURANCE', datetime('now'), datetime('now'))
                """,
                (f"JOB-A{i:03d}",),
            )
    facts = collect_assurance_facts(ctx, "PRJ-003")
    assert facts.get("qa_jobs_total") == 20
    assert facts.get("reviews_total") == 16
    assert facts.get("qa_jobs_total") != facts.get("reviews_total")


def test_worker_failure_evidence_leaves_run_recoverable(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_handoff_run(conn, run_id="RUN-WORK")
        apply_event_to_run(
            conn,
            run_id="RUN-WORK",
            event_type="WORK_FAILED",
            payload={"phase": "REMEDIATION", "actor_id": "developer-agent", "progress": 50},
        )
        run = conn.execute(
            "SELECT status FROM execution_runs WHERE run_id = 'RUN-WORK'"
        ).fetchone()
        blocked = conn.execute(
            "SELECT 1 FROM projectos_events WHERE run_id = 'RUN-WORK' AND event_type = 'RUN_BLOCKED'"
        ).fetchone()
    assert run["status"] == "RUNNING"
    assert blocked is None


def test_only_pm_terminal_events_close_run(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_handoff_run(conn, run_id="RUN-PM")
        for event_type in ("QA_GATE_FAILED", "PACKAGE_FAILED", "RELEASE_PREPARATION_BLOCKED"):
            apply_event_to_run(
                conn,
                run_id="RUN-PM",
                event_type=event_type,
                payload={"phase": "QA_GATE", "actor_id": "qa-agent", "progress": 40},
            )
        run = conn.execute(
            "SELECT status FROM execution_runs WHERE run_id = 'RUN-PM'"
        ).fetchone()
    assert run["status"] == "RUNNING"


def test_sponsor_directive_during_active_run_same_run_id(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        "projectos.pm_agent.orchestrate_release_capability",
        lambda *args, **kwargs: "mock",
    )
    with connection(ctx.db_path) as conn:
        first = accept_sponsor_handoff(
            ctx,
            conn,
            handoff=_release_handoff(),
            project_id="PRJ-003",
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
            sponsor_user_id="U1",
        )
        follow_up = HandoffRequest(
            project_id="PRJ-003",
            objective="Investigate the context error and resolve it.",
            action_type="work_request",
            rationale="",
            scope="",
            constraints="{}",
            acceptance_intent="",
            exclusions="",
            source_conversation_summary="",
        )
        second = accept_sponsor_handoff(
            ctx,
            conn,
            handoff=follow_up,
            project_id="PRJ-003",
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
            sponsor_user_id="U1",
        )
        count = conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
    assert second.run_id == first.run_id
    assert int(count) == 1
