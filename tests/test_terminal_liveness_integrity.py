"""Terminal integrity, liveness, and recovery regressions."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.acceptance_contract import build_acceptance_contract
from projectos.db import connection
from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.event_dispatcher import claim_pending_outbox, dispatch_event_outbox, mark_subscriber_blocked
from projectos.execution_run import create_execution_run, update_execution_run
from projectos.migrate import initialize_database
from projectos.recover import run_recovery
from projectos.run_liveness import assert_nonterminal_run_has_durable_next_action
from projectos.sponsor_outcome import evaluate_sponsor_outcome
from projectos.store import acquire_lease, create_job, get_job, list_jobs_by_statuses
from projectos.sponsor_handoff import create_sponsor_handoff, mark_handoff_accepted


def _ctx(tmp_path: Path):
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    repo_root = str(repo.resolve())
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": repo_root, "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return db, repo_root


def test_release_vacuous_success_blocked(tmp_path: Path) -> None:
    db, _ = _ctx(tmp_path)
    with connection(db) as conn:
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
        evaluation = evaluate_sponsor_outcome(
            conn,
            run_id=run.run_id,
            handoff_id=handoff.handoff_id,
            objective="ship",
            request_type="RELEASE",
            release_record_id=None,
        )
    assert evaluation.satisfied is False
    assert "release_record" in evaluation.missing_outputs or evaluation.missing_outputs


def test_release_acceptance_contract_never_empty(tmp_path: Path) -> None:
    db, _ = _ctx(tmp_path)
    with connection(db) as conn:
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
        contract = build_acceptance_contract(
            conn,
            handoff_id=handoff.handoff_id,
            request_type="RELEASE",
            objective="ship",
        )
    assert contract.effective_requirements
    assert "release_record" in contract.effective_requirements


def test_release_record_id_propagated_in_event(tmp_path: Path) -> None:
    db, _ = _ctx(tmp_path)
    with connection(db) as conn:
        emit_projectos_event(
            conn,
            ctx=EventContext(
                project_id="PRJ-003",
                run_id="RUN-1",
                release_id="REL-001",
                release_record_id="DLV-REC-1",
            ),
            event_type="RELEASE_PREPARED",
            summary="prepared",
            actor_id=ACTOR_PM,
            evidence={"release_record_id": "DLV-REC-1"},
            subscribers=(),
        )
        row = conn.execute(
            "SELECT release_record_id FROM projectos_events WHERE run_id = 'RUN-1' ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
    assert row["release_record_id"] == "DLV-REC-1"


def test_publish_without_verify_gate_rejected(tmp_path: Path) -> None:
    from projectos.delivery.service import DeliveryService
    from projectos.delivery.store import insert_delivery_release, new_release_record_id, upsert_gate_status
    from projectos.services.context import ServiceContext

    db, repo_root = _ctx(tmp_path)
    ctx = ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")
    repo = Path(repo_root)
    (repo / "project").mkdir(exist_ok=True)
    import json
    from projectos.delivery.contract import infer_delivery_contract

    (repo / "project" / "delivery.json").write_text(
        json.dumps(
            infer_delivery_contract(
                product_name="Gamma",
                repository_owner="acme",
                repository_name="gamma",
                target_platforms=["windows-x64"],
                external_distribution=False,
            )
        ),
        encoding="utf-8",
    )
    svc = DeliveryService(ctx)
    record_id = new_release_record_id()
    with connection(db) as conn:
        insert_delivery_release(
            conn,
            release_record_id=record_id,
            project_human_id="PRJ-003",
            release_human_id="REL-001",
            version="1.0.0",
            candidate_git_sha="sha1",
            lifecycle_status="packaged",
        )
        upsert_gate_status(
            conn,
            release_record_id=record_id,
            gate_name="PACKAGE_GATE",
            status="passed",
        )
    with pytest.raises(OrchestrationError, match="verification"):
        svc.publish_release(record_id)


def test_concurrent_job_claim_single_owner(tmp_path: Path) -> None:
    db, repo_root = _ctx(tmp_path)
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-RACE",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
        )
        acquire_lease(conn, get_job(conn, job.id), worker_id="w1", lease_seconds=60)
        with pytest.raises(OrchestrationError):
            acquire_lease(conn, get_job(conn, job.id), worker_id="w2", lease_seconds=60)


def test_outbox_causal_hold_does_not_increment_attempts(tmp_path: Path) -> None:
    db, _ = _ctx(tmp_path)
    with connection(db) as conn:
        conn.execute(
            """
            INSERT INTO projectos_events (
                event_id, project_id, actor_type, actor_id, actor_role,
                event_type, summary
            ) VALUES ('EVT-A', 'PRJ-003', 'agent', 'pm-agent', 'PM', 'TEST_A', 'a')
            """
        )
        conn.execute(
            """
            INSERT INTO projectos_events (
                event_id, project_id, actor_type, actor_id, actor_role,
                event_type, summary
            ) VALUES ('EVT-B', 'PRJ-003', 'agent', 'pm-agent', 'PM', 'TEST_B', 'b')
            """
        )
        conn.execute(
            """
            INSERT INTO event_outbox (event_id, subscriber, idempotency_key, payload_json, status, attempts)
            VALUES ('EVT-A', 'slack', 'slack:EVT-A', '{"run_id":"R1","slack_channel_id":"C1"}', 'pending', 0)
            """
        )
        conn.execute(
            """
            INSERT INTO event_outbox (event_id, subscriber, idempotency_key, payload_json, status, attempts)
            VALUES ('EVT-B', 'slack', 'slack:EVT-B', '{"run_id":"R1","slack_channel_id":"C1"}', 'pending', 0)
            """
        )
        rows = claim_pending_outbox(conn, subscriber="slack", claimed_by="d1", limit=2)
        assert len(rows) == 2
        mark_subscriber_blocked(conn, outbox_id=int(rows[1]["id"]), blocked_by_outbox_id=int(rows[0]["id"]), error="hold")
        b = conn.execute("SELECT attempts, status FROM event_outbox WHERE event_id = 'EVT-B'").fetchone()
    assert b["status"] == "blocked"
    assert int(b["attempts"]) == 0


def test_recovery_builds_service_context(tmp_path: Path) -> None:
    db, repo_root = _ctx(tmp_path)
    write_registry = tmp_path / "projects.json"
    from projectos.services.context import ServiceContext

    ctx = ServiceContext(db_path=db, registry_path=write_registry)
    report = run_recovery(db_path=db, registry_path=write_registry, service_ctx=ctx)
    assert report is not None
