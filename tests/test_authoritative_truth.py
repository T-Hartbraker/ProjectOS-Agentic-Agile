"""Authoritative truth, run lineage, and durable liveness regressions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.acceptance_contract import build_acceptance_contract, evaluate_effective_requirements
from projectos.db import connection
from projectos.delivery.contract import infer_delivery_contract
from projectos.delivery.store import (
    insert_delivery_release,
    new_release_record_id,
    update_delivery_release,
    upsert_gate_status,
)
from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event, lookup_event_context_for_job
from projectos.errors import OrchestrationError
from projectos.execution_run import create_execution_run
from projectos.migrate import initialize_database
from projectos.run_liveness import assert_nonterminal_run_has_durable_next_action
from projectos.run_next_actions import has_durable_next_action, list_active_next_actions, persist_run_next_action
from projectos.sponsor_handoff import create_sponsor_handoff
from projectos.sponsor_outcome import evaluate_sponsor_outcome
from projectos.store import (
    create_job,
    get_job,
    mark_failure,
    mark_succeeded,
    promote_retry_wait_to_ready,
    set_job_source_provenance,
    utc_now,
)


def _ctx(tmp_path: Path):
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    repo_root = str(repo.resolve())
    registry = tmp_path / "projects.json"
    write_registry(
        registry,
        [{"project_human_id": "PRJ-003", "repository_root": repo_root, "enabled": True}],
    )
    (repo / "project").mkdir(exist_ok=True)
    (repo / "project" / "delivery.json").write_text(
        json.dumps(
            infer_delivery_contract(
                product_name="Gamma",
                repository_owner="acme",
                repository_name="gamma",
                target_platforms=["windows-x64"],
                external_distribution=True,
            )
        ),
        encoding="utf-8",
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return db, repo_root, registry


def _release_handoff(conn, *, desired: dict | None = None):
    handoff = create_sponsor_handoff(
        conn,
        project_id="PRJ-003",
        team_id="T1",
        channel_id="C1",
        thread_ts="1.0",
        sponsor_user_id="U1",
        request_type="RELEASE",
        objective="publish release",
        desired_outputs_json=json.dumps(desired or {"publish": True}),
    )
    run = create_execution_run(
        conn,
        project_id="PRJ-003",
        handoff_id=handoff.handoff_id,
        request_type="RELEASE",
        objective="publish release",
    )
    return handoff, run


def test_candidate_provenance_mismatch_fails(tmp_path: Path) -> None:
    db, _, registry = _ctx(tmp_path)
    record_id = new_release_record_id()
    with connection(db) as conn:
        handoff, run = _release_handoff(conn)
        insert_delivery_release(
            conn,
            release_record_id=record_id,
            project_human_id="PRJ-003",
            release_human_id="REL-001",
            version="1.0.0",
            candidate_git_sha="sha-b",
            lifecycle_status="verified",
        )
        contract = build_acceptance_contract(
            conn,
            handoff_id=handoff.handoff_id,
            request_type="RELEASE",
            objective="publish",
        )
        satisfied, _, _, missing, _ = evaluate_effective_requirements(
            conn,
            contract=contract,
            release_record_id=record_id,
            candidate_git_sha="sha-a",
        )
        evaluation = evaluate_sponsor_outcome(
            conn,
            run_id=run.run_id,
            handoff_id=handoff.handoff_id,
            objective="publish",
            request_type="RELEASE",
            release_record_id=record_id,
            candidate_git_sha="sha-a",
            project_id="PRJ-003",
            registry_path=registry,
        )
    assert satisfied is False
    assert "candidate_provenance" in missing
    assert evaluation.satisfied is False
    assert "candidate_provenance" in evaluation.missing_outputs


def test_candidate_provenance_match_satisfied(tmp_path: Path) -> None:
    db, _, registry = _ctx(tmp_path)
    record_id = new_release_record_id()
    with connection(db) as conn:
        handoff, run = _release_handoff(conn)
        insert_delivery_release(
            conn,
            release_record_id=record_id,
            project_human_id="PRJ-003",
            release_human_id="REL-001",
            version="1.0.0",
            candidate_git_sha="sha-a",
            lifecycle_status="verified",
        )
        upsert_gate_status(conn, release_record_id=record_id, gate_name="VERIFY_GATE", status="passed")
        upsert_gate_status(conn, release_record_id=record_id, gate_name="QA_GATE", status="passed")
        contract = build_acceptance_contract(
            conn,
            handoff_id=handoff.handoff_id,
            request_type="RELEASE",
            objective="publish",
        )
        satisfied, _, ok, missing, _ = evaluate_effective_requirements(
            conn,
            contract=contract,
            release_record_id=record_id,
            candidate_git_sha="sha-a",
        )
    assert "candidate_provenance" in ok or satisfied
    assert "candidate_provenance" not in missing


def test_release_without_candidate_identity_fails(tmp_path: Path) -> None:
    db, _, registry = _ctx(tmp_path)
    record_id = new_release_record_id()
    with connection(db) as conn:
        handoff, run = _release_handoff(conn)
        insert_delivery_release(
            conn,
            release_record_id=record_id,
            project_human_id="PRJ-003",
            release_human_id="REL-001",
            version="1.0.0",
            candidate_git_sha="sha-a",
            lifecycle_status="verified",
        )
        evaluation = evaluate_sponsor_outcome(
            conn,
            run_id=run.run_id,
            handoff_id=handoff.handoff_id,
            objective="publish",
            request_type="RELEASE",
            release_record_id=record_id,
            candidate_git_sha=None,
            project_id="PRJ-003",
            registry_path=registry,
        )
    assert evaluation.satisfied is False
    assert "candidate_provenance" in evaluation.missing_outputs


def test_delivery_policy_required_at_terminal_boundary(tmp_path: Path) -> None:
    db, _, registry = _ctx(tmp_path)
    record_id = new_release_record_id()
    with connection(db) as conn:
        handoff, run = _release_handoff(conn, desired={"publish": True})
        insert_delivery_release(
            conn,
            release_record_id=record_id,
            project_human_id="PRJ-003",
            release_human_id="REL-001",
            version="1.0.0",
            candidate_git_sha="sha-a",
            lifecycle_status="verified",
        )
        update_delivery_release(
            conn,
            record_id,
            publication_status="published",
            github_release_url="https://example.com/r",
        )
        upsert_gate_status(conn, release_record_id=record_id, gate_name="VERIFY_GATE", status="passed")
        upsert_gate_status(conn, release_record_id=record_id, gate_name="QA_GATE", status="passed")
        upsert_gate_status(conn, release_record_id=record_id, gate_name="PUBLICATION_GATE", status="passed")
        missing_sbom = evaluate_sponsor_outcome(
            conn,
            run_id=run.run_id,
            handoff_id=handoff.handoff_id,
            objective="publish",
            request_type="RELEASE",
            release_record_id=record_id,
            candidate_git_sha="sha-a",
            project_id="PRJ-003",
            registry_path=registry,
        )
        unavailable = evaluate_sponsor_outcome(
            conn,
            run_id=run.run_id,
            handoff_id=handoff.handoff_id,
            objective="publish",
            request_type="RELEASE",
            release_record_id=record_id,
            candidate_git_sha="sha-a",
            project_id="PRJ-UNKNOWN",
            registry_path=registry,
        )
    assert missing_sbom.satisfied is False
    assert "sbom" in missing_sbom.missing_outputs
    assert unavailable.satisfied is False
    assert "delivery_policy" in unavailable.missing_outputs


def test_fake_qa_gate_passed_rejected(tmp_path: Path) -> None:
    db, repo_root, _ = _ctx(tmp_path)
    with connection(db) as conn:
        handoff, run = _release_handoff(conn)
        with pytest.raises(OrchestrationError, match="authoritative"):
            emit_projectos_event(
                conn,
                ctx=EventContext(project_id="PRJ-003", run_id=run.run_id),
                event_type="QA_GATE_PASSED",
                summary="spoof",
                actor_id=ACTOR_PM,
                evidence={"gate": "PASSED", "candidate_git_sha": "sha-a", "run_id": run.run_id},
            )


def test_fake_work_completed_rejected(tmp_path: Path) -> None:
    db, repo_root, _ = _ctx(tmp_path)
    with connection(db) as conn:
        handoff, run = _release_handoff(conn)
        with pytest.raises(OrchestrationError, match="persisted remediation work"):
            emit_projectos_event(
                conn,
                ctx=EventContext(project_id="PRJ-003", run_id=run.run_id),
                event_type="WORK_COMPLETED",
                summary="spoof",
                actor_id=ACTOR_PM,
                evidence={
                    "work_item_id": "WRK-FAKE",
                    "candidate_git_sha": "sha-a",
                },
            )


def test_release_published_requires_actual_publication(tmp_path: Path) -> None:
    db, _, _ = _ctx(tmp_path)
    record_id = new_release_record_id()
    with connection(db) as conn:
        handoff, run = _release_handoff(conn)
        insert_delivery_release(
            conn,
            release_record_id=record_id,
            project_human_id="PRJ-003",
            release_human_id="REL-001",
            version="1.0.0",
            candidate_git_sha="sha-a",
            lifecycle_status="packaged",
        )
        update_delivery_release(conn, record_id, publication_status="pending")
        with pytest.raises(OrchestrationError, match="publication"):
            emit_projectos_event(
                conn,
                ctx=EventContext(
                    project_id="PRJ-003",
                    run_id=run.run_id,
                    release_record_id=record_id,
                ),
                event_type="RELEASE_PUBLISHED",
                summary="spoof",
                actor_id=ACTOR_PM,
                evidence={"release_record_id": record_id},
            )


def test_liveness_run_scoped_not_project_scoped(tmp_path: Path) -> None:
    db, repo_root, _ = _ctx(tmp_path)
    with connection(db) as conn:
        handoff_a, run_a = _release_handoff(conn)
        handoff_b, run_b = _release_handoff(conn)
        create_job(
            conn,
            human_id="JOB-B-ONLY",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            run_id=run_b.run_id,
        )
    with connection(db) as conn:
        assert has_durable_next_action(conn, run_id=run_b.run_id, project_id="PRJ-003") is True
        assert has_durable_next_action(conn, run_id=run_a.run_id, project_id="PRJ-003") is False


def test_event_context_bound_to_job_run_not_latest(tmp_path: Path) -> None:
    db, repo_root, _ = _ctx(tmp_path)
    with connection(db) as conn:
        _, run_a = _release_handoff(conn)
        _, run_b = _release_handoff(conn)
        job_a = create_job(
            conn,
            human_id="JOB-A-ASSURANCE",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="QA",
            queue="ASSURANCE_SECURITY",
            status="READY",
            run_id=run_a.run_id,
        )
        ctx = lookup_event_context_for_job(conn, job_a.id)
    assert ctx is not None
    assert ctx.run_id == run_a.run_id
    assert ctx.run_id != run_b.run_id


def test_next_action_completed_when_job_terminal(tmp_path: Path) -> None:
    db, repo_root, _ = _ctx(tmp_path)
    with connection(db) as conn:
        _, run = _release_handoff(conn)
        job = create_job(
            conn,
            human_id="JOB-COMPLETE",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            run_id=run.run_id,
        )
        persist_run_next_action(
            conn,
            run_id=run.run_id,
            project_id="PRJ-003",
            action_type="EXECUTABLE_JOB",
            orchestration_job_id=job.id,
        )
        mark_succeeded(conn, job.id, output_ref="out", candidate_git_sha="sha-a")
        active = list_active_next_actions(conn, run_id=run.run_id)
    assert active == []


def test_retry_wait_future_not_promoted(tmp_path: Path) -> None:
    db, repo_root, _ = _ctx(tmp_path)
    future = (utc_now() + timedelta(hours=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-RETRY-FUTURE",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="RETRY_WAIT",
            retry_at=future,
        )
        with pytest.raises(OrchestrationError, match="future"):
            promote_retry_wait_to_ready(conn, job.id, now=utc_now())


def test_retry_wait_past_promoted(tmp_path: Path) -> None:
    db, repo_root, _ = _ctx(tmp_path)
    past = (utc_now() - timedelta(minutes=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-RETRY-PAST",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="RETRY_WAIT",
            retry_at=past,
        )
        promoted = promote_retry_wait_to_ready(conn, job.id, now=utc_now())
    assert promoted.status == "READY"


def test_exact_release_record_not_latest_project_release(tmp_path: Path) -> None:
    db, _, registry = _ctx(tmp_path)
    record_a = new_release_record_id()
    record_b = new_release_record_id()
    with connection(db) as conn:
        handoff, run = _release_handoff(conn)
        insert_delivery_release(
            conn,
            release_record_id=record_a,
            project_human_id="PRJ-003",
            release_human_id="REL-A",
            version="1.0.0",
            candidate_git_sha="sha-a",
            lifecycle_status="verified",
        )
        update_delivery_release(
            conn,
            record_a,
            publication_status="published",
            github_release_url="https://example.com/a",
        )
        upsert_gate_status(conn, release_record_id=record_a, gate_name="VERIFY_GATE", status="passed")
        upsert_gate_status(conn, release_record_id=record_a, gate_name="QA_GATE", status="passed")
        upsert_gate_status(conn, release_record_id=record_a, gate_name="SBOM_GATE", status="passed")
        upsert_gate_status(conn, release_record_id=record_a, gate_name="CHECKSUM_GATE", status="passed")
        upsert_gate_status(conn, release_record_id=record_a, gate_name="SIGNATURE_GATE", status="passed")
        upsert_gate_status(conn, release_record_id=record_a, gate_name="PUBLICATION_GATE", status="passed")
        insert_delivery_release(
            conn,
            release_record_id=record_b,
            project_human_id="PRJ-003",
            release_human_id="REL-B",
            version="2.0.0",
            candidate_git_sha="sha-b",
            lifecycle_status="packaged",
        )
        eval_a = evaluate_sponsor_outcome(
            conn,
            run_id=run.run_id,
            handoff_id=handoff.handoff_id,
            objective="publish",
            request_type="RELEASE",
            release_record_id=record_a,
            candidate_git_sha="sha-a",
            project_id="PRJ-003",
            registry_path=registry,
        )
        eval_b = evaluate_sponsor_outcome(
            conn,
            run_id=run.run_id,
            handoff_id=handoff.handoff_id,
            objective="publish",
            request_type="RELEASE",
            release_record_id=record_b,
            candidate_git_sha="sha-b",
            project_id="PRJ-003",
            registry_path=registry,
        )
    assert eval_a.satisfied is False
    assert eval_b.satisfied is False
